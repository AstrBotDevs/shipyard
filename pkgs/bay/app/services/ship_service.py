import aiohttp
import asyncio
import logging
from typing import Optional, List, Dict
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
        # First, check if this session has a stopped ship with existing data
        stopped_ship = await db_service.find_stopped_ship_for_session(session_id)
        if stopped_ship and docker_service.ship_data_exists(stopped_ship.id):
            # Restore the stopped ship
            logger.info(
                f"Restoring stopped ship {stopped_ship.id} for session {session_id}"
            )
            return await self._restore_ship(stopped_ship, request, session_id)

        # Second, try to find an available ship that can accept this session
        available_ship = await db_service.find_available_ship(session_id)

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
                # Add this session to the ship and extend TTL
                session_ship = SessionShip(
                    session_id=session_id, ship_id=available_ship.id
                )
                await db_service.create_session_ship(session_ship)
                await db_service.increment_ship_session_count(available_ship.id)

                # Extend TTL by adding the new session's TTL to existing TTL
                extended_ttl = available_ship.ttl + request.ttl
                available_ship.ttl = extended_ttl
                available_ship = await db_service.update_ship(available_ship)

                # Reschedule cleanup with extended TTL
                await self._schedule_cleanup(available_ship.id, extended_ttl)

                logger.info(
                    f"Session {session_id} joined ship {available_ship.id}, TTL extended to {extended_ttl}s"
                )
                return available_ship

        # No available ship found, create a new one
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
            ship.current_session_num = 1  # First session
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
            session_ship = SessionShip(session_id=session_id, ship_id=ship.id)
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
        return await db_service.get_ship(ship_id)

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
            await self._extend_ttl_after_operation(ship_id)

        return result

    async def get_logs(self, ship_id: str) -> str:
        """Get ship container logs"""
        ship = await db_service.get_ship(ship_id)
        if not ship or not ship.container_id:
            return ""

        return await docker_service.get_container_logs(ship.container_id)

    async def list_active_ships(self) -> List[Ship]:
        """List all active ships"""
        return await db_service.list_active_ships()

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
            await self._extend_ttl_after_operation(ship_id)

        return result

    async def _extend_ttl_after_operation(self, ship_id: str):
        """Extend ship TTL after an operation"""
        from datetime import datetime, timezone

        ship = await db_service.get_ship(ship_id)
        if not ship or ship.status == 0:
            return

        # Calculate new TTL: extend_ttl_after_ops seconds from now
        new_ttl = settings.extend_ttl_after_ops
        ship.ttl = new_ttl
        await db_service.update_ship(ship)

        # Reschedule cleanup with new TTL
        await self._schedule_cleanup(ship_id, new_ttl)

        logger.info(f"Ship {ship_id} TTL extended to {new_ttl}s after operation")

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

            # Schedule TTL cleanup
            await self._schedule_cleanup(ship.id, ship.ttl)

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


# Global ship service instance
ship_service = ShipService()
