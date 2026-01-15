from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool
from typing import Optional, List
from app.config import settings
from app.models import Ship, SessionShip
from datetime import datetime, timezone


class DatabaseService:
    def __init__(self):
        self.engine: Optional[AsyncEngine] = None

    async def initialize(self):
        """Initialize database connection"""
        self.engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            future=True,
            # SQLite specific settings
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

    async def create_tables(self):
        """Create database tables"""
        if not self.engine:
            await self.initialize()

        async with self.engine.begin() as conn:  # type: ignore
            await conn.run_sync(SQLModel.metadata.create_all)

    def get_session(self) -> AsyncSession:
        """Get database session"""
        if not self.engine:
            raise RuntimeError("Database not initialized")

        return AsyncSession(self.engine, expire_on_commit=False)

    async def create_ship(self, ship: Ship) -> Ship:
        """Create a new ship record"""
        session = self.get_session()
        try:
            session.add(ship)
            await session.commit()
            await session.refresh(ship)
            return ship
        finally:
            await session.close()

    async def get_ship(self, ship_id: str) -> Optional[Ship]:
        """Get ship by ID"""
        session = self.get_session()
        try:
            statement = select(Ship).where(Ship.id == ship_id)
            result = await session.execute(statement)
            return result.scalar_one_or_none()
        finally:
            await session.close()

    async def update_ship(self, ship: Ship) -> Ship:
        """Update ship record"""
        ship.updated_at = datetime.now(timezone.utc)
        session = self.get_session()
        try:
            session.add(ship)
            await session.commit()
            await session.refresh(ship)
            return ship
        finally:
            await session.close()

    async def delete_ship(self, ship_id: str) -> bool:
        """Delete ship by ID"""
        session = self.get_session()
        try:
            statement = select(Ship).where(Ship.id == ship_id)
            result = await session.execute(statement)
            ship = result.scalar_one_or_none()

            if ship:
                await session.delete(ship)
                await session.commit()
                return True
            return False
        finally:
            await session.close()

    async def list_active_ships(self) -> List[Ship]:
        """List all active ships"""
        session = self.get_session()
        try:
            statement = select(Ship).where(Ship.status == 1)
            result = await session.execute(statement)
            return list(result.scalars().all())
        finally:
            await session.close()

    async def count_active_ships(self) -> int:
        """Count active ships"""
        ships = await self.list_active_ships()
        return len(ships)

    # SessionShip operations
    async def create_session_ship(self, session_ship: SessionShip) -> SessionShip:
        """Create a new session-ship relationship"""
        session = self.get_session()
        try:
            session.add(session_ship)
            await session.commit()
            await session.refresh(session_ship)
            return session_ship
        finally:
            await session.close()

    async def get_session_ship(
        self, session_id: str, ship_id: str
    ) -> Optional[SessionShip]:
        """Get session-ship relationship"""
        session = self.get_session()
        try:
            statement = select(SessionShip).where(
                SessionShip.session_id == session_id, SessionShip.ship_id == ship_id
            )
            result = await session.execute(statement)
            return result.scalar_one_or_none()
        finally:
            await session.close()

    async def get_sessions_for_ship(self, ship_id: str) -> List[SessionShip]:
        """Get all sessions for a ship"""
        session = self.get_session()
        try:
            statement = select(SessionShip).where(SessionShip.ship_id == ship_id)
            result = await session.execute(statement)
            return list(result.scalars().all())
        finally:
            await session.close()

    async def update_session_activity(
        self, session_id: str, ship_id: str
    ) -> Optional[SessionShip]:
        """Update last activity for a session"""
        session = self.get_session()
        try:
            statement = select(SessionShip).where(
                SessionShip.session_id == session_id, SessionShip.ship_id == ship_id
            )
            result = await session.execute(statement)
            session_ship = result.scalar_one_or_none()

            if session_ship:
                session_ship.last_activity = datetime.now(timezone.utc)
                session.add(session_ship)
                await session.commit()
                await session.refresh(session_ship)

            return session_ship
        finally:
            await session.close()

    async def update_session_ship(self, session_ship: SessionShip) -> SessionShip:
        """Update session-ship relationship"""
        session = self.get_session()
        try:
            session.add(session_ship)
            await session.commit()
            await session.refresh(session_ship)
            return session_ship
        finally:
            await session.close()

    async def find_available_ship(self, session_id: str) -> Optional[Ship]:
        """Find an available ship that can accept a new session"""
        session = self.get_session()
        try:
            # Find ships that have available session slots
            statement = select(Ship).where(
                Ship.status == 1, Ship.current_session_num < Ship.max_session_num
            )
            result = await session.execute(statement)
            ships = list(result.scalars().all())

            # Check if this session already has access to any ship
            for ship in ships:
                existing_session = await self.get_session_ship(session_id, ship.id)
                if existing_session:
                    return ship

            # Return the first available ship
            return ships[0] if ships else None
        finally:
            await session.close()

    async def find_active_ship_for_session(self, session_id: str) -> Optional[Ship]:
        """Find an active running ship that this session has access to"""
        session = self.get_session()
        try:
            # Find active ships that this session has access to
            statement = (
                select(Ship)
                .join(SessionShip, Ship.id == SessionShip.ship_id)
                .where(
                    SessionShip.session_id == session_id,
                    Ship.status == 1,
                )
            )
            result = await session.execute(statement)
            return result.scalar_one_or_none()
        finally:
            await session.close()

    async def find_stopped_ship_for_session(self, session_id: str) -> Optional[Ship]:
        """Find a stopped ship that belongs to this session"""
        session = self.get_session()
        try:
            # Find stopped ships that this session has access to
            statement = (
                select(Ship)
                .join(SessionShip, Ship.id == SessionShip.ship_id)
                .where(
                    SessionShip.session_id == session_id,
                    Ship.status == 0,
                )
            )
            result = await session.execute(statement)
            return result.scalar_one_or_none()
        finally:
            await session.close()

    async def increment_ship_session_count(self, ship_id: str) -> Optional[Ship]:
        """Increment the current session count for a ship"""
        session = self.get_session()
        try:
            statement = select(Ship).where(Ship.id == ship_id)
            result = await session.execute(statement)
            ship = result.scalar_one_or_none()

            if ship:
                ship.current_session_num += 1
                ship.updated_at = datetime.now(timezone.utc)
                session.add(ship)
                await session.commit()
                await session.refresh(ship)

            return ship
        finally:
            await session.close()

    async def decrement_ship_session_count(self, ship_id: str) -> Optional[Ship]:
        """Decrement the current session count for a ship"""
        session = self.get_session()
        try:
            statement = select(Ship).where(Ship.id == ship_id)
            result = await session.execute(statement)
            ship = result.scalar_one_or_none()

            if ship and ship.current_session_num > 0:
                ship.current_session_num -= 1
                ship.updated_at = datetime.now(timezone.utc)
                session.add(ship)
                await session.commit()
                await session.refresh(ship)

            return ship
        finally:
            await session.close()


db_service = DatabaseService()
