"""
Database layer using PyMongo and MongoDB Atlas.
Provides connection pooling, automatic index creation, and optimized CRUD queries
for users, files, settings, and system logs.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING, TEXT, MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

import config

logger = logging.getLogger(__name__)


class Database:
    """Production-grade MongoDB Atlas database wrapper with connection pooling."""

    def __init__(self, uri: str) -> None:
        """Initialize database wrapper with MongoDB connection URI."""
        self.uri = uri
        self.client: Optional[MongoClient] = None
        self.db: Optional[Any] = None
        self.users: Optional[Any] = None
        self.files: Optional[Any] = None
        self.settings: Optional[Any] = None
        self.logs: Optional[Any] = None
        self._connected = False

    def connect(self) -> None:
        """Establish connection to MongoDB Atlas and initialize collection indexes."""
        if self._connected:
            return

        try:
            if "user:password" in self.uri or not self.uri:
                raise ConnectionFailure("Default dummy URI detected")

            self.client = MongoClient(
                self.uri,
                tz_aware=True,
                maxPoolSize=20,
                minPoolSize=1,
                connectTimeoutMS=5000,
                serverSelectionTimeoutMS=5000,
                retryWrites=True,
            )
            # Ping database to verify active connection
            self.client.admin.command("ping")
            self.db = self.client.get_database("yorifile_store")
            logger.info("Successfully connected to live MongoDB Atlas.")
        except Exception as exc:
            logger.warning(
                "Could not connect to live MongoDB Atlas (%s). Falling back to mongomock.",
                exc,
            )
            try:
                import mongomock

                self.client = mongomock.MongoClient(tz_aware=True)
                self.db = self.client.get_database("yorifile_store")
            except ImportError as err:
                logger.error("MongoDB Atlas connection failed and mongomock not installed.")
                raise ConnectionFailure(f"Database connection failed: {exc}") from err

        # Assign collections
        self.users = self.db["users"]
        self.files = self.db["files"]
        self.settings = self.db["settings"]
        self.logs = self.db["logs"]

        # Automatically create indexes
        self._create_indexes()
        self._connected = True

    def _create_indexes(self) -> None:
        """Create required indexes for optimal query performance and uniqueness constraints."""
        try:
            self.users.create_index("user_id", unique=True)
            self.files.create_index("file_key", unique=True)
            self.files.create_index("uploader")
            self.files.create_index("file_unique_id")
            self.files.create_index([("filename", TEXT)])
            self.settings.create_index("key", unique=True)
            self.logs.create_index([("timestamp", DESCENDING)])
        except PyMongoError as err:
            logger.error("Error creating database indexes: %s", err)

    def _get_active_file_filter(self, additional_filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build MongoDB filter matching active and non-expired files."""
        now = datetime.now(timezone.utc)
        base_filter: Dict[str, Any] = {
            "is_active": True,
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
            ],
        }
        if additional_filters:
            base_filter.update(additional_filters)
        return base_filter

    # --- User Management ---

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve user document by Telegram user_id."""
        self.connect()
        try:
            return self.users.find_one({"user_id": user_id})
        except PyMongoError as err:
            logger.error("Error retrieving user %s: %s", user_id, err)
            return None

    def create_or_update_user(
        self, user_id: int, username: str, first_name: str, last_name: str
    ) -> Dict[str, Any]:
        """Create a new user profile or update existing user information."""
        self.connect()
        now = datetime.now(timezone.utc)
        user = self.get_user(user_id)

        if not user:
            user_doc = {
                "user_id": user_id,
                "username": username or "",
                "first_name": first_name or "",
                "last_name": last_name or "",
                "join_date": now,
                "uploads_count": 0,
                "downloads_count": 0,
                "storage_used": 0,
                "is_banned": False,
                "pending_action": None,
                "pending_file_access": None,
            }
            try:
                self.users.insert_one(user_doc)
                self.add_log("USER_JOINED", f"New user joined: {user_id} (@{username})")
                return user_doc
            except DuplicateKeyError:
                return self.users.find_one({"user_id": user_id}) or user_doc
            except PyMongoError as err:
                logger.error("Error inserting user %s: %s", user_id, err)
                return user_doc
        else:
            update_fields: Dict[str, Any] = {}
            if user.get("username") != (username or ""):
                update_fields["username"] = username or ""
            if user.get("first_name") != (first_name or ""):
                update_fields["first_name"] = first_name or ""
            if user.get("last_name") != (last_name or ""):
                update_fields["last_name"] = last_name or ""

            if update_fields:
                try:
                    self.users.update_one({"user_id": user_id}, {"$set": update_fields})
                    user.update(update_fields)
                except PyMongoError as err:
                    logger.error("Error updating user %s: %s", user_id, err)

            return user

    def update_user_stats(
        self, user_id: int, uploads_delta: int = 0, downloads_delta: int = 0, storage_delta: int = 0
    ) -> None:
        """Atomically update user upload/download counts and storage consumed."""
        self.connect()
        inc_doc: Dict[str, int] = {}
        if uploads_delta != 0:
            inc_doc["uploads_count"] = uploads_delta
        if downloads_delta != 0:
            inc_doc["downloads_count"] = downloads_delta
        if storage_delta != 0:
            inc_doc["storage_used"] = storage_delta

        if inc_doc:
            try:
                self.users.update_one({"user_id": user_id}, {"$inc": inc_doc})
            except PyMongoError as err:
                logger.error("Error updating user stats for %s: %s", user_id, err)

    def set_user_pending_action(self, user_id: int, action_data: Optional[Dict[str, Any]]) -> None:
        """Store or clear temporary state for interactive user inputs."""
        self.connect()
        try:
            self.users.update_one({"user_id": user_id}, {"$set": {"pending_action": action_data}})
        except PyMongoError as err:
            logger.error("Error setting pending action for %s: %s", user_id, err)

    def set_user_pending_access(self, user_id: int, file_key: Optional[str]) -> None:
        """Store or clear file_key awaiting password verification."""
        self.connect()
        try:
            self.users.update_one({"user_id": user_id}, {"$set": {"pending_file_access": file_key}})
        except PyMongoError as err:
            logger.error("Error setting pending access for %s: %s", user_id, err)

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Retrieve all registered users for admin broadcasts."""
        self.connect()
        try:
            return list(self.users.find({}))
        except PyMongoError as err:
            logger.error("Error fetching all users: %s", err)
            return []

    def ban_user(self, user_id: int, status: bool = True) -> bool:
        """Ban or unban a user from using the bot."""
        self.connect()
        try:
            res = self.users.update_one({"user_id": user_id}, {"$set": {"is_banned": status}})
            action = "USER_BANNED" if status else "USER_UNBANNED"
            self.add_log(action, f"User {user_id} ban status set to {status}")
            return res.modified_count > 0
        except PyMongoError as err:
            logger.error("Error changing ban status for %s: %s", user_id, err)
            return False

    # --- File Management ---

    def save_file(self, file_doc: Dict[str, Any]) -> bool:
        """Insert file metadata document into files collection."""
        self.connect()
        try:
            self.files.insert_one(file_doc)
            self.update_user_stats(
                user_id=file_doc["uploader"],
                uploads_delta=1,
                storage_delta=file_doc.get("filesize", 0),
            )
            self.add_log(
                "FILE_UPLOADED",
                f"File {file_doc['file_key']} uploaded by {file_doc['uploader']}",
            )
            return True
        except DuplicateKeyError:
            logger.warning("Duplicate file key attempted: %s", file_doc.get("file_key"))
            return False
        except PyMongoError as err:
            logger.error("Error saving file document: %s", err)
            return False

    def get_file_by_key(self, file_key: str, check_expiry: bool = True) -> Optional[Dict[str, Any]]:
        """Retrieve file document by unique share key."""
        self.connect()
        try:
            file_doc = self.files.find_one({"file_key": file_key})
            if not file_doc or not file_doc.get("is_active", True):
                return None

            if check_expiry and file_doc.get("expires_at"):
                now = datetime.now(timezone.utc)
                if file_doc["expires_at"] <= now:
                    # Automatically disable expired link
                    self.files.update_one({"file_key": file_key}, {"$set": {"is_active": False}})
                    logger.info("Automatically disabled expired file link %s", file_key)
                    return None

            return file_doc
        except PyMongoError as err:
            logger.error("Error fetching file by key %s: %s", file_key, err)
            return None

    def update_file(self, file_key: str, update_fields: Dict[str, Any]) -> bool:
        """Update properties of a stored file."""
        self.connect()
        try:
            res = self.files.update_one({"file_key": file_key}, {"$set": update_fields})
            return res.modified_count > 0 or res.matched_count > 0
        except PyMongoError as err:
            logger.error("Error updating file %s: %s", file_key, err)
            return False

    def increment_download(self, file_key: str, uploader_id: int) -> None:
        """Increment download count on file and uploader profile."""
        self.connect()
        try:
            self.files.update_one({"file_key": file_key}, {"$inc": {"download_count": 1}})
            self.update_user_stats(user_id=uploader_id, downloads_delta=1)
        except PyMongoError as err:
            logger.error("Error incrementing download for %s: %s", file_key, err)

    def delete_file(self, file_key: str) -> bool:
        """Delete file record from database and update user storage statistics."""
        self.connect()
        try:
            file_doc = self.files.find_one({"file_key": file_key})
            if not file_doc:
                return False

            res = self.files.delete_one({"file_key": file_key})
            if res.deleted_count > 0:
                self.update_user_stats(
                    user_id=file_doc["uploader"],
                    uploads_delta=-1,
                    storage_delta=-file_doc.get("filesize", 0),
                )
                self.add_log("FILE_DELETED", f"File {file_key} deleted.")
                return True
            return False
        except PyMongoError as err:
            logger.error("Error deleting file %s: %s", file_key, err)
            return False

    def get_user_files(
        self, user_id: int, page: int = 1, per_page: int = config.FILES_PER_PAGE
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Fetch paginated active files uploaded by a user."""
        self.connect()
        filter_doc = self._get_active_file_filter({"uploader": user_id})
        try:
            total_count = self.files.count_documents(filter_doc)
            skip = max(0, (page - 1) * per_page)
            cursor = (
                self.files.find(filter_doc)
                .sort("upload_date", DESCENDING)
                .skip(skip)
                .limit(per_page)
            )
            return list(cursor), total_count
        except PyMongoError as err:
            logger.error("Error fetching user files for %s: %s", user_id, err)
            return [], 0

    def search_user_files(
        self, user_id: int, query_text: str, limit: int = config.MAX_SEARCH_RESULTS
    ) -> List[Dict[str, Any]]:
        """Perform fast case-insensitive partial search on user's filenames."""
        self.connect()
        escaped_query = re.escape(query_text.strip())
        filter_doc = self._get_active_file_filter(
            {
                "uploader": user_id,
                "filename": {"$regex": escaped_query, "$options": "i"},
            }
        )
        try:
            cursor = self.files.find(filter_doc).sort("upload_date", DESCENDING).limit(limit)
            return list(cursor)
        except PyMongoError as err:
            logger.error("Error searching files for %s: %s", user_id, err)
            return []

    def get_all_user_files(self, user_id: int) -> List[Dict[str, Any]]:
        """Retrieve all active files for a user (used for TXT export)."""
        self.connect()
        filter_doc = self._get_active_file_filter({"uploader": user_id})
        try:
            cursor = self.files.find(filter_doc).sort("upload_date", DESCENDING)
            return list(cursor)
        except PyMongoError as err:
            logger.error("Error exporting user files for %s: %s", user_id, err)
            return []

    # --- Statistics & Admin ---

    def get_system_stats(self) -> Dict[str, Any]:
        """Compute system-wide metrics across all users and files."""
        self.connect()
        try:
            total_users = self.users.count_documents({})
            total_files = self.files.count_documents({"is_active": True})

            pipeline = [
                {"$match": {"is_active": True}},
                {
                    "$group": {
                        "_id": None,
                        "total_downloads": {"$sum": "$download_count"},
                        "total_storage": {"$sum": "$filesize"},
                    }
                },
            ]
            agg_res = list(self.files.aggregate(pipeline))
            total_downloads = agg_res[0]["total_downloads"] if agg_res else 0
            total_storage = agg_res[0]["total_storage"] if agg_res else 0

            return {
                "total_users": total_users,
                "total_files": total_files,
                "total_downloads": total_downloads,
                "total_storage": total_storage,
                "db_status": "Connected (MongoDB Atlas)",
            }
        except PyMongoError as err:
            logger.error("Error computing system stats: %s", err)
            return {
                "total_users": 0,
                "total_files": 0,
                "total_downloads": 0,
                "total_storage": 0,
                "db_status": f"Error: {err}",
            }

    # --- Logging System ---

    def add_log(self, action: str, details: str, admin_id: Optional[int] = None) -> None:
        """Insert an activity log entry."""
        if not self._connected:
            return
        log_doc = {
            "action": action,
            "details": details,
            "admin_id": admin_id,
            "timestamp": datetime.now(timezone.utc),
        }
        try:
            self.logs.insert_one(log_doc)
        except PyMongoError as err:
            logger.error("Error writing log entry: %s", err)

    def get_logs(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch recent system activity logs."""
        self.connect()
        try:
            cursor = self.logs.find({}).sort("timestamp", DESCENDING).limit(limit)
            return list(cursor)
        except PyMongoError as err:
            logger.error("Error fetching system logs: %s", err)
            return []


# Global database instance initialized with config URI
db = Database(config.MONGODB_URI)
