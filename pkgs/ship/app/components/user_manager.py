"""
User management for session isolation.

This module provides utilities for creating and managing isolated Linux users
for each session, ensuring complete filesystem and process isolation.
"""

import asyncio
import logging
import os
import pwd
import grp
import shutil
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# 存储session到用户名的映射
session_users: Dict[str, str] = {}

# 用户ID范围（从10000开始，避免与系统用户冲突）
USER_ID_START = 10000
USER_ID_COUNTER = USER_ID_START

# 用户组名
USER_GROUP = "shipyard_users"
USER_GROUP_ID = 9999


@dataclass
class ProcessResult:
    success: bool
    stdout: str
    stderr: str
    return_code: Optional[int] = None
    pid: Optional[int] = None
    error: Optional[str] = None


class UserManager:
    """管理session用户的类"""

    @staticmethod
    async def ensure_shipyard_group():
        """确保shipyard用户组存在"""
        try:
            grp.getgrnam(USER_GROUP)
            logger.info(f"Group {USER_GROUP} already exists")
        except KeyError:
            # 创建用户组
            process = await asyncio.create_subprocess_exec(
                "groupadd",
                "-g",
                str(USER_GROUP_ID),
                USER_GROUP,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create group {USER_GROUP}: {stderr.decode()}",
                )
            logger.info(f"Created group {USER_GROUP} with GID {USER_GROUP_ID}")

    @staticmethod
    async def create_session_user(session_id: str) -> str:
        """为session创建独立的Linux用户"""
        global USER_ID_COUNTER

        if session_id in session_users:
            return session_users[session_id]

        # 确保用户组存在
        await UserManager.ensure_shipyard_group()

        # 生成用户名（基于session_id，但限制长度和字符）
        username = f"ship_{session_id[:8]}"
        user_id = USER_ID_COUNTER
        USER_ID_COUNTER += 1

        # 检查用户是否已存在
        try:
            pwd.getpwnam(username)
            logger.info(f"User {username} already exists")
            session_users[session_id] = username
            return username
        except KeyError:
            pass

        # 创建用户主目录
        home_dir = f"/home/{username}"

        # 创建用户
        process = await asyncio.create_subprocess_exec(
            "useradd",
            "-u",
            str(user_id),  # 用户ID
            "-g",
            USER_GROUP,  # 主用户组
            "-d",
            home_dir,  # 主目录
            "-m",  # 创建主目录
            "-s",
            "/bin/bash",  # 默认shell
            username,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            if "already exists" in error_msg:
                logger.info(f"User {username} already exists")
                session_users[session_id] = username
                return username
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create user {username}: {error_msg}",
                )

        logger.info(f"Created user {username} with UID {user_id}")

        # 设置用户主目录权限
        await UserManager.setup_user_workspace(username, home_dir)

        session_users[session_id] = username
        return username

    @staticmethod
    async def setup_user_workspace(username: str, home_dir: str):
        """设置用户的工作空间"""
        home_path = Path(home_dir)
        home_path.mkdir(parents=True, exist_ok=True)
        workspace_dir = home_path / "workspace"
        workspace_dir.mkdir(exist_ok=True)

        # 获取用户信息
        user_info = pwd.getpwnam(username)
        user_id = user_info.pw_uid
        group_id = user_info.pw_gid

        # 设置权限
        shutil.chown(home_path, user=user_id, group=group_id)
        shutil.chown(workspace_dir, user=user_id, group=group_id)
        os.chmod(home_path, 0o755)
        os.chmod(workspace_dir, 0o755)

        # 创建基本的shell配置文件
        bashrc_path = home_path / ".bashrc"
        if not bashrc_path.exists():
            bashrc_content = """
# Basic shell configuration for shipyard user
export PS1='\\u@shipyard:\\w\\$ '
export PATH=/usr/local/bin:/usr/bin:/bin
cd ~/workspace
"""
            bashrc_path.write_text(bashrc_content)
            shutil.chown(bashrc_path, user=user_id, group=group_id)
            os.chmod(bashrc_path, 0o644)

        logger.info(f"Set up workspace for user {username} at {workspace_dir}")

    @staticmethod
    def get_session_user(session_id: str) -> Optional[str]:
        """获取session对应的用户名"""
        return session_users.get(session_id)

    @staticmethod
    async def get_user_info(username: str) -> Dict:
        """获取用户信息"""
        try:
            user_info = pwd.getpwnam(username)
            return {
                "username": username,
                "uid": user_info.pw_uid,
                "gid": user_info.pw_gid,
                "home_dir": user_info.pw_dir,
                "shell": user_info.pw_shell,
            }
        except KeyError:
            raise HTTPException(status_code=404, detail=f"User {username} not found")

    @staticmethod
    async def cleanup_session_user(session_id: str) -> bool:
        """清理session用户"""
        username = session_users.get(session_id)
        if not username:
            return False

        try:
            # 终止用户的所有进程
            process = await asyncio.create_subprocess_exec(
                "pkill",
                "-u",
                username,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()

            if session_id in session_users:
                del session_users[session_id]

            logger.info(f"Cleaned up session user {username} for session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to cleanup user {username}: {e}")
            return False


async def get_or_create_session_user(session_id: str) -> str:
    """获取或创建session对应的用户"""
    username = UserManager.get_session_user(session_id)
    if username:
        return username

    return await UserManager.create_session_user(session_id)


async def run_as_user(
    username: str,
    command: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    shell: bool = True,
    background: bool = False,
) -> ProcessResult:
    """以指定用户身份运行命令"""
    try:
        _ = await get_or_create_session_user(username)
        user_info = await UserManager.get_user_info(username)
        user_home = user_info["home_dir"]

        # 准备环境变量
        process_env = {
            "HOME": user_home,
            "USER": username,
            "LOGNAME": username,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "SHELL": "/bin/bash",
        }
        if env:
            process_env.update(env)

        working_dir = Path(user_info["home_dir"]) / "workspace"
        if cwd:
            if not os.path.isabs(cwd):
                working_dir = working_dir / cwd
            else:
                working_dir = Path(cwd)
            # resolve working dir
            working_dir = working_dir.resolve()
            try:
                working_dir.relative_to(Path(user_info["home_dir"]) / "workspace")
            except ValueError:
                raise HTTPException(
                    status_code=403,
                    detail=f"Access denied: path must be within user workspace: {user_info['home_dir']}/workspace",
                )

        # 使用 sudo 切换用户执行命令
        sudo_command = f"sudo -u {username} -H bash -c 'cd {working_dir} && {command}'"

        if shell:
            process = await asyncio.create_subprocess_shell(
                sudo_command,
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(sudo_command),
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        if background:
            return ProcessResult(
                success=True,
                return_code=0,
                stdout="",
                stderr="",
                pid=process.pid,
            )
        else:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                return ProcessResult(
                    success=process.returncode == 0,
                    return_code=process.returncode,
                    stdout=stdout.decode().strip(),
                    stderr=stderr.decode().strip(),
                    pid=process.pid,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return ProcessResult(
                    success=False,
                    return_code=-1,
                    stdout="",
                    stderr="",
                    pid=process.pid,
                    error="Command timed out",
                )

    except Exception as e:
        return ProcessResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr="",
            error=str(e),
            pid=None,
        )
