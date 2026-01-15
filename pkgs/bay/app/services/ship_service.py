import aiohttp
import asyncio
import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta, timezone
from app.config import settings
from app.models import (
    Ship,
    CreateShipRequest,
    ExecRequest,
    ExecResponse,
    SessionShip,
    UploadFileResponse,
)
from app.database import db_service
from app.services.docker_service import docker_service

logger = logging.getLogger(__name__)


class ShipService:
    """Service for managing Ship lifecycle and operations"""

    def __init__(self):
        # Track cleanup tasks for each ship to enable cancellation
        self._cleanup_tasks: Dict[str, asyncio.Task] = {}

    async def create_ship(self, request: CreateShipRequest, session_id: str) -> Ship:
        """Create a new ship or reuse an existing one for the session"""
        # First, check if this session already has an active running ship
        active_ship = await db_service.find_active_ship_for_session(session_id)
        if active_ship:
            # Verify that the container actually exists and is running
            if active_ship.container_id and await docker_service.is_container_running(
                active_ship.container_id
            ):
                # Update last activity and return the existing active ship
                await db_service.update_session_activity(session_id, active_ship.id)
                logger.info(
                    f"Session {session_id} already has active ship {active_ship.id}, returning it"
                )
                return active_ship
            else:
                # Container doesn't exist or isn't running, mark ship as stopped and restore it
                logger.warning(
                    f"Ship {active_ship.id} is marked active but container is not running, restoring..."
                )
                active_ship.status = 0
                await db_service.update_ship(active_ship)
                # Restore the ship
                return await self._restore_ship(active_ship, request, session_id)

        # Second, check if this session has a stopped ship with existing data
        stopped_ship = await db_service.find_stopped_ship_for_session(session_id)
        if stopped_ship and docker_service.ship_data_exists(stopped_ship.id):
            # Restore the stopped ship
            logger.info(
                f"Restoring stopped ship {stopped_ship.id} for session {session_id}"
            )
            return await self._restore_ship(stopped_ship, request, session_id)

        # Third, try to find an available ship that can accept this session
        available_ship = await db_service.find_available_ship(session_id)

        if available_ship:
            # Verify that the container actually exists and is running
            if (
                not available_ship.container_id
                or not await docker_service.is_container_running(
                    available_ship.container_id
                )
            ):
                # Container doesn't exist or isn't running, mark ship as stopped
                logger.warning(
                    f"Ship {available_ship.id} is marked active but container is not running, marking as stopped"
                )
                available_ship.status = 0
                await db_service.update_ship(available_ship)
                # Don't use this ship, continue to create a new one
                available_ship = None

        if available_ship:
            # Check if this session already has access to this ship
            existing_session = await db_service.get_session_ship(
                session_id, available_ship.id
            )

            if existing_session:
                # Update last activity and return existing ship
                await db_service.update_session_activity(session_id, available_ship.id)
                return available_ship
            else:
                # Calculate expiration time for this session
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=request.ttl)

                # Add this session to the ship
                session_ship = SessionShip(
                    session_id=session_id,
                    ship_id=available_ship.id,
                    expires_at=expires_at,
                    initial_ttl=request.ttl,
                )
                await db_service.create_session_ship(session_ship)
                await db_service.increment_ship_session_count(available_ship.id)

                # Recalculate ship's TTL based on all sessions' expiration times
                await self._recalculate_and_schedule_cleanup(available_ship.id)

                logger.info(
                    f"Session {session_id} joined ship {available_ship.id}, expires at {expires_at}"
                )
                return available_ship

        # Fourth, no available ship found, create a new one
        # Check ship limits
        if settings.behavior_after_max_ship == "reject":
            active_count = await db_service.count_active_ships()
            if active_count >= settings.max_ship_num:
                raise ValueError("Maximum number of ships reached")
        elif settings.behavior_after_max_ship == "wait":
            # Wait for available slot
            await self._wait_for_available_slot()

        # Create ship record
        ship = Ship(ttl=request.ttl, max_session_num=request.max_session_num)
        ship = await db_service.create_ship(ship)

        try:
            # Create container
            container_info = await docker_service.create_ship_container(
                ship, request.spec
            )

            # Update ship with container info
            ship.container_id = container_info["container_id"]
            ship.ip_address = container_info["ip_address"]
            ship = await db_service.update_ship(ship)

            # Wait for ship to be ready
            if not ship.ip_address:
                logger.error(f"Ship {ship.id} has no IP address")
                await db_service.delete_ship(ship.id)
                raise RuntimeError("Ship has no IP address")

            logger.info(f"Waiting for ship {ship.id} to become ready...")
            is_ready = await self._wait_for_ship_ready(ship.ip_address)

            if not is_ready:
                # Ship failed to become ready, cleanup
                logger.error(f"Ship {ship.id} failed health check, cleaning up")
                if ship.container_id:
                    await docker_service.stop_ship_container(ship.container_id)
                await db_service.delete_ship(ship.id)
                raise RuntimeError(
                    f"Ship failed to become ready within {settings.ship_health_check_timeout} seconds"
                )

            # Create session-ship relationship
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=request.ttl)
            session_ship = SessionShip(
                session_id=session_id,
                ship_id=ship.id,
                expires_at=expires_at,
                initial_ttl=request.ttl,
            )
            await db_service.create_session_ship(session_ship)
            await db_service.increment_ship_session_count(ship.id)

            # Schedule TTL cleanup
            await self._schedule_cleanup(ship.id, ship.ttl)

            logger.info(f"Ship {ship.id} created successfully and is ready")
            return ship

        except Exception as e:
            # Cleanup on failure
            await db_service.delete_ship(ship.id)
            logger.error(f"Failed to create ship {ship.id}: {e}")
            raise

    async def get_ship(self, ship_id: str) -> Optional[Ship]:
        """Get ship by ID"""
        ship = await db_service.get_ship(ship_id)
        if ship:
            # Calculate and set the actual expiration time based on all sessions
            await self._set_ship_expires_at(ship)
        return ship

    async def delete_ship(self, ship_id: str) -> bool:
        """Delete ship"""
        ship = await db_service.get_ship(ship_id)
        if not ship:
            return False

        # Cancel cleanup task if exists
        if ship_id in self._cleanup_tasks:
            task = self._cleanup_tasks[ship_id]
            if not task.done():
                task.cancel()
            del self._cleanup_tasks[ship_id]

        # Stop container if exists
        if ship.container_id:
            try:
                await docker_service.stop_ship_container(ship.container_id)
            except Exception as e:
                logger.error(f"Failed to stop container for ship {ship_id}: {e}")

        # Delete from database
        return await db_service.delete_ship(ship_id)

    async def extend_ttl(self, ship_id: str, new_ttl: int) -> Optional[Ship]:
        """Extend ship TTL"""
        ship = await db_service.get_ship(ship_id)
        if not ship or ship.status == 0:
            return None

        ship.ttl = new_ttl
        ship = await db_service.update_ship(ship)

        # Reschedule cleanup
        await self._schedule_cleanup(ship_id, new_ttl)

        return ship

    async def execute_operation(
        self, ship_id: str, request: ExecRequest, session_id: str
    ) -> ExecResponse:
        """Execute operation on ship"""
        ship = await db_service.get_ship(ship_id)
        if not ship or ship.status == 0:
            return ExecResponse(success=False, error="Ship not found or not running")

        if not ship.ip_address:
            return ExecResponse(success=False, error="Ship IP address not available")

        # Verify that this session has access to this ship
        session_ship = await db_service.get_session_ship(session_id, ship_id)
        if not session_ship:
            return ExecResponse(
                success=False, error="Session does not have access to this ship"
            )

        # Update last activity for this session
        await db_service.update_session_activity(session_id, ship_id)

        # Forward request to ship container
        result = await self._forward_to_ship(ship.ip_address, request, session_id)

        # Extend TTL after successful operation
        if result.success:
            await self._extend_ttl_after_operation(ship_id, session_id)

        return result

    async def get_logs(self, ship_id: str) -> str:
        """Get ship container logs"""
        ship = await db_service.get_ship(ship_id)
        if not ship or not ship.container_id:
            return ""

        return await docker_service.get_container_logs(ship.container_id)

    async def list_active_ships(self) -> List[Ship]:
        """List all active ships"""
        ships = await db_service.list_active_ships()
        # Calculate and set the actual expiration time for each ship
        for ship in ships:
            await self._set_ship_expires_at(ship)
        return ships

    async def upload_file(
        self, ship_id: str, file_content: bytes, file_path: str, session_id: str
    ) -> UploadFileResponse:
        """Upload file to ship container"""
        # Check file size limit
        if len(file_content) > settings.max_upload_size:
            return UploadFileResponse(
                success=False,
                error=f"File size ({len(file_content)} bytes) exceeds maximum allowed size ({settings.max_upload_size} bytes)",
                message="File upload failed due to size limit",
            )

        ship = await db_service.get_ship(ship_id)
        if not ship or ship.status == 0:
            return UploadFileResponse(
                success=False,
                error="Ship not found or not running",
                message="File upload failed",
            )

        if not ship.ip_address:
            return UploadFileResponse(
                success=False,
                error="Ship IP address not available",
                message="File upload failed",
            )

        # Verify that this session has access to this ship
        session_ship = await db_service.get_session_ship(session_id, ship_id)
        if not session_ship:
            return UploadFileResponse(
                success=False,
                error="Session does not have access to this ship",
                message="File upload failed",
            )

        # Update last activity for this session
        await db_service.update_session_activity(session_id, ship_id)

        # Forward file upload to ship container
        result = await self._upload_file_to_ship(
            ship.ip_address, file_content, file_path, session_id
        )

        # Extend TTL after successful upload
        if result.success:
            await self._extend_ttl_after_operation(ship_id, session_id)

        return result

    async def download_file(
        self, ship_id: str, file_path: str, session_id: str
    ) -> tuple[bool, bytes, str]:
        """Download file from ship container

        Returns:
            tuple: (success, file_content, error_message)
        """
        ship = await db_service.get_ship(ship_id)
        if not ship or ship.status == 0:
            return (False, b"", "Ship not found or not running")

        if not ship.ip_address:
            return (False, b"", "Ship IP address not available")

        # Verify that this session has access to this ship
        session_ship = await db_service.get_session_ship(session_id, ship_id)
        if not session_ship:
            return (False, b"", "Session does not have access to this ship")

        # Update last activity for this session
        await db_service.update_session_activity(session_id, ship_id)

        # Forward file download request to ship container
        success, file_content, error = await self._download_file_from_ship(
            ship.ip_address, file_path, session_id
        )

        # Extend TTL after successful download
        if success:
            await self._extend_ttl_after_operation(ship_id, session_id)

        return (success, file_content, error)

    async def _extend_ttl_after_operation(self, ship_id: str, session_id: str):
        """Extend ship TTL after an operation by refreshing the current session's expiration time"""
        # Get the session information
        session_ship = await db_service.get_session_ship(session_id, ship_id)
        if not session_ship:
            logger.warning(f"Session {session_id} not found for ship {ship_id}")
            return

        # Refresh this session's expiration time using its initial TTL
        new_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=session_ship.initial_ttl
        )
        session_ship.expires_at = new_expires_at
        await db_service.update_session_ship(session_ship)

        # Recalculate ship's cleanup time based on all sessions
        await self._recalculate_and_schedule_cleanup(ship_id)

        logger.info(
            f"Session {session_id} TTL refreshed for ship {ship_id}, new expires_at: {new_expires_at}"
        )

    async def _recalculate_and_schedule_cleanup(self, ship_id: str):
        """Recalculate ship's TTL based on all sessions' expiration times and reschedule cleanup"""
        # Get all sessions for this ship
        all_sessions = await db_service.get_sessions_for_ship(ship_id)

        if not all_sessions:
            logger.warning(f"No sessions found for ship {ship_id}")
            return

        # Find the maximum expiration time among all sessions
        max_expires_at = max(s.expires_at for s in all_sessions)

        # Ensure max_expires_at is timezone-aware
        if max_expires_at.tzinfo is None:
            max_expires_at = max_expires_at.replace(tzinfo=timezone.utc)

        # Calculate remaining time until expiration
        now = datetime.now(timezone.utc)
        remaining_seconds = (max_expires_at - now).total_seconds()

        # Make sure remaining_seconds is not negative
        if remaining_seconds < 0:
            remaining_seconds = 0

        # Update ship's TTL in database for reference
        ship = await db_service.get_ship(ship_id)
        if ship:
            ship.ttl = int(remaining_seconds)
            await db_service.update_ship(ship)

        # Reschedule cleanup
        await self._schedule_cleanup(ship_id, int(remaining_seconds))

        logger.info(
            f"Ship {ship_id} TTL recalculated: {remaining_seconds}s (expires at {max_expires_at})"
        )

    async def _set_ship_expires_at(self, ship: Ship):
        """Calculate and set ship's expiration time based on all sessions"""
        if ship.status == 0:
            # Stopped ships don't have an expiration time
            ship.expires_at = None
            return

        # Get all sessions for this ship
        all_sessions = await db_service.get_sessions_for_ship(ship.id)

        if not all_sessions:
            # No sessions, ship expires immediately (or already expired)
            ship.expires_at = None
            return

        # Find the maximum expiration time among all sessions
        max_expires_at = max(s.expires_at for s in all_sessions)

        # Ensure max_expires_at is timezone-aware
        if max_expires_at.tzinfo is None:
            max_expires_at = max_expires_at.replace(tzinfo=timezone.utc)

        ship.expires_at = max_expires_at

    async def _wait_for_available_slot(self):
        """Wait for an available ship slot"""
        max_wait_time = 300  # 5 minutes
        check_interval = 5  # 5 seconds
        waited = 0

        while waited < max_wait_time:
            active_count = await db_service.count_active_ships()
            if active_count < settings.max_ship_num:
                return

            await asyncio.sleep(check_interval)
            waited += check_interval

        raise TimeoutError("Timeout waiting for available ship slot")

    async def _schedule_cleanup(self, ship_id: str, ttl: int):
        """Schedule ship cleanup after TTL expires"""
        # Cancel any existing cleanup task for this ship
        if ship_id in self._cleanup_tasks:
            old_task = self._cleanup_tasks[ship_id]
            if not old_task.done():
                old_task.cancel()
            del self._cleanup_tasks[ship_id]

        # Create and store new cleanup task
        task = asyncio.create_task(self._cleanup_ship_after_delay(ship_id, ttl))
        self._cleanup_tasks[ship_id] = task
        return task

    async def _cleanup_ship_after_delay(self, ship_id: str, ttl: int):
        """Perform ship cleanup after TTL delay"""
        try:
            await asyncio.sleep(ttl)

            ship = await db_service.get_ship(ship_id)
            if ship and ship.status == 1:
                # Mark as stopped
                ship.status = 0
                await db_service.update_ship(ship)

                # Stop container (but keep ship_data directory)
                if ship.container_id:
                    await docker_service.stop_ship_container(ship.container_id)

                logger.info(f"Ship {ship_id} cleaned up after TTL expiration")
        except asyncio.CancelledError:
            logger.info(f"Cleanup task for ship {ship_id} was cancelled")
            raise
        except Exception as e:
            logger.error(f"Failed to cleanup ship {ship_id}: {e}")
        finally:
            # Remove task from tracking
            if ship_id in self._cleanup_tasks:
                del self._cleanup_tasks[ship_id]

    async def _restore_ship(
        self, ship: Ship, request: CreateShipRequest, session_id: str
    ) -> Ship:
        """Restore a stopped ship by recreating its container"""
        try:
            # Recreate container with existing ship data
            container_info = await docker_service.create_ship_container(
                ship, request.spec
            )

            # Update ship with new container info
            ship.container_id = container_info["container_id"]
            ship.ip_address = container_info["ip_address"]
            ship.status = 1  # Mark as running
            ship.ttl = request.ttl  # Update TTL
            ship = await db_service.update_ship(ship)

            # Wait for ship to be ready
            if not ship.ip_address:
                logger.error(f"Restored ship {ship.id} has no IP address")
                raise RuntimeError("Ship has no IP address")

            logger.info(f"Waiting for restored ship {ship.id} to become ready...")
            is_ready = await self._wait_for_ship_ready(ship.ip_address)

            if not is_ready:
                # Ship failed to become ready, cleanup
                logger.error(f"Restored ship {ship.id} failed health check")
                if ship.container_id:
                    await docker_service.stop_ship_container(ship.container_id)
                ship.status = 0
                await db_service.update_ship(ship)
                raise RuntimeError(
                    f"Ship failed to become ready within {settings.ship_health_check_timeout} seconds"
                )

            # Update last activity for this session
            await db_service.update_session_activity(session_id, ship.id)

            # Get the session_ship to refresh its expiration time
            session_ship = await db_service.get_session_ship(session_id, ship.id)
            if session_ship:
                # Refresh this session's expiration time with new TTL
                new_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=request.ttl
                )
                session_ship.expires_at = new_expires_at
                session_ship.initial_ttl = request.ttl
                await db_service.update_session_ship(session_ship)

            # Recalculate and schedule TTL cleanup
            await self._recalculate_and_schedule_cleanup(ship.id)

            logger.info(
                f"Ship {ship.id} restored successfully for session {session_id}"
            )
            return ship

        except Exception as e:
            # Mark ship as stopped on failure
            ship.status = 0
            await db_service.update_ship(ship)
            logger.error(f"Failed to restore ship {ship.id}: {e}")
            raise

    async def _forward_to_ship(
        self, ship_ip: str, request: ExecRequest, session_id: str
    ) -> ExecResponse:
        """Forward request to ship container"""
        url = f"http://{ship_ip}:8123/{request.type}"

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            headers = {"X-SESSION-ID": session_id}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, json=request.payload or {}, headers=headers
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return ExecResponse(success=True, data=data)
                    else:
                        error_text = await response.text()
                        return ExecResponse(
                            success=False,
                            error=f"Ship returned {response.status}: {error_text}",
                        )

        except aiohttp.ClientError as e:
            logger.error(f"Failed to forward request to ship {ship_ip}: {e}")
            return ExecResponse(success=False, error=f"Connection error: {str(e)}")
        except asyncio.TimeoutError:
            return ExecResponse(success=False, error="Request timeout")
        except Exception as e:
            logger.error(f"Unexpected error forwarding to ship {ship_ip}: {e}")
            return ExecResponse(success=False, error=f"Internal error: {str(e)}")

    async def _wait_for_ship_ready(self, ship_ip: str) -> bool:
        """Wait for ship to be ready by polling /health endpoint"""
        health_url = f"http://{ship_ip}:8123/health"
        max_wait_time = settings.ship_health_check_timeout
        check_interval = settings.ship_health_check_interval
        waited = 0

        logger.info(f"Starting health check for ship at {ship_ip}")

        while waited < max_wait_time:
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(health_url) as response:
                        if response.status == 200:
                            logger.info(f"Ship at {ship_ip} is ready after {waited}s")
                            return True
            except Exception as e:
                logger.debug(f"Health check failed for {ship_ip}: {e}")

            await asyncio.sleep(check_interval)
            waited += check_interval

        logger.error(
            f"Ship at {ship_ip} failed to become ready within {max_wait_time}s"
        )
        return False

    async def _upload_file_to_ship(
        self, ship_ip: str, file_content: bytes, file_path: str, session_id: str
    ) -> UploadFileResponse:
        """Upload file to ship container via HTTP API using multipart/form-data"""
        url = f"http://{ship_ip}:8123/upload"

        try:
            # Create multipart form data
            data = aiohttp.FormData()
            data.add_field(
                "file",
                file_content,
                filename="upload",
                content_type="application/octet-stream",
            )
            data.add_field("file_path", file_path)

            timeout = aiohttp.ClientTimeout(total=120)  # 2 minutes for file upload
            headers = {"X-SESSION-ID": session_id}

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data, headers=headers) as response:
                    if response.status == 200:
                        resp = await response.json()
                        return UploadFileResponse(
                            success=True,
                            message="File uploaded successfully",
                            file_path=resp.get("file_path", "unknown"),
                        )
                    else:
                        error_text = await response.text()
                        return UploadFileResponse(
                            success=False,
                            error=f"Ship returned {response.status}: {error_text}",
                            message="File upload failed",
                        )

        except aiohttp.ClientError as e:
            logger.error(f"Failed to upload file to ship {ship_ip}: {e}")
            return UploadFileResponse(
                success=False,
                error=f"Connection error: {str(e)}",
                message="File upload failed",
            )
        except asyncio.TimeoutError:
            return UploadFileResponse(
                success=False, error="File upload timeout", message="File upload failed"
            )
        except Exception as e:
            logger.error(f"Unexpected error uploading file to ship {ship_ip}: {e}")
            return UploadFileResponse(
                success=False,
                error=f"Internal error: {str(e)}",
                message="File upload failed",
            )

    async def _download_file_from_ship(
        self, ship_ip: str, file_path: str, session_id: str
    ) -> tuple[bool, bytes, str]:
        """Download file from ship container via HTTP API

        Returns:
            tuple: (success, file_content, error_message)
        """
        url = f"http://{ship_ip}:8123/download"

        try:
            timeout = aiohttp.ClientTimeout(total=120)  # 2 minutes for file download
            headers = {"X-SESSION-ID": session_id}
            params = {"file_path": file_path}

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 200:
                        file_content = await response.read()
                        return (True, file_content, "")
                    else:
                        error_text = await response.text()
                        return (
                            False,
                            b"",
                            f"Ship returned {response.status}: {error_text}",
                        )

        except aiohttp.ClientError as e:
            logger.error(f"Failed to download file from ship {ship_ip}: {e}")
            return (False, b"", f"Connection error: {str(e)}")
        except asyncio.TimeoutError:
            return (False, b"", "File download timeout")
        except Exception as e:
            logger.error(f"Unexpected error downloading file from ship {ship_ip}: {e}")
            return (False, b"", f"Internal error: {str(e)}")


# Global ship service instance
ship_service = ShipService()
