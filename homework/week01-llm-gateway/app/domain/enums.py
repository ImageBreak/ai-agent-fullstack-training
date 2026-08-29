from enum import StrEnum


class Status(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    ERROR = "error"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
