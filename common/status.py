from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    DISPATCHING = "DISPATCHING"
    TRANSFERRING = "TRANSFERRING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PACKAGING = "PACKAGING"
    RESULT_TRANSFERRING = "RESULT_TRANSFERRING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.UPLOADING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.UPLOADING: {TaskStatus.UPLOADED, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.UPLOADED: {TaskStatus.DISPATCHING, TaskStatus.QUEUED, TaskStatus.FAILED},
    TaskStatus.DISPATCHING: {TaskStatus.TRANSFERRING, TaskStatus.QUEUED, TaskStatus.RETRYING, TaskStatus.FAILED},
    TaskStatus.TRANSFERRING: {TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.RETRYING, TaskStatus.FAILED},
    TaskStatus.QUEUED: {TaskStatus.DISPATCHING, TaskStatus.PROCESSING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.PROCESSING: {
        TaskStatus.PACKAGING,
        TaskStatus.RESULT_TRANSFERRING,
        TaskStatus.RETRYING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.FAILED,
    },
    TaskStatus.PACKAGING: {TaskStatus.RESULT_TRANSFERRING, TaskStatus.FAILED},
    TaskStatus.RESULT_TRANSFERRING: {TaskStatus.SUCCEEDED, TaskStatus.RETRYING, TaskStatus.FAILED},
    TaskStatus.RETRYING: {TaskStatus.DISPATCHING, TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.FAILED},
    TaskStatus.CANCEL_REQUESTED: {TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

