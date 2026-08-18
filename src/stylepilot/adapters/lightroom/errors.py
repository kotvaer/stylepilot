class LightroomBridgeError(RuntimeError):
    """Base error for failures at the Lightroom integration boundary."""


class LightroomConnectionError(LightroomBridgeError):
    """The MCP server or Lightroom plugin could not complete a request."""


class LightroomProtocolError(LightroomBridgeError):
    """The MCP server returned a response that violates the expected contract."""


class LightroomCapabilityError(LightroomBridgeError):
    """The connected plugin does not expose a safety-critical capability."""


class LightroomRollbackError(LightroomBridgeError):
    """A guarded Develop write failed and its compensating rollback also failed."""

    def __init__(
        self,
        photo_id: str,
        apply_error: Exception,
        rollback_error: Exception,
    ) -> None:
        self.photo_id = photo_id
        self.apply_error = apply_error
        self.rollback_error = rollback_error
        super().__init__(
            f"Develop write failed for {photo_id} and automatic rollback also failed: "
            f"apply={apply_error}; rollback={rollback_error}"
        )


class LightroomApprovalTimeoutError(LightroomBridgeError):
    """The Lightroom review panel did not receive a decision before its deadline."""
