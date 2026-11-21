"""The verifier watching an agent that has a shell in the same container.

The plain-agent baseline runs in the task container, where the verifier's snapshot also
lives (/tmp/verimend-verify). VeriMend's tools cannot leave the project, but a shell can
go anywhere. So after the snapshot this seals it with a hash of everything `verify`
reads, and before the check it hashes again and uploads the verifier's source afresh. A
snapshot that changed in between is an error, never a verdict (decision 0007).

The agent can still read the snapshot. It holds nothing but a copy of the project it
already has, and `mentions_verifier` records whether it looked.
"""

from __future__ import annotations

from typing import Any

from verimend.tools.verify import CONTAINER_SOURCE, CONTAINER_WORK, WorkspaceVerifier

# Paths relative to the work folder, sorted bytewise, so the hash is the same every time.
SEAL_SCRIPT = (
    'cd "$1" && find base state.json -type f -print0 | LC_ALL=C sort -z '
    "| xargs -0 sha256sum | sha256sum"
)


class SnapshotModified(RuntimeError):
    """The verifier's snapshot is not what it sealed. A harness error, not a verdict."""


class SealedVerifier:
    def __init__(self, inner: WorkspaceVerifier) -> None:
        self.inner = inner
        self.seal: str | None = None

    async def _hash(self) -> str:
        done = await self.inner.workspace.exec(
            ["sh", "-c", SEAL_SCRIPT, "sh", self.inner.work_dir], timeout=600
        )
        digest = done.output.strip().split(" ", 1)[0]
        if done.code != 0 or len(digest) != 64:
            raise SnapshotModified(f"the snapshot could not be hashed: {done.output[-300:]}")
        return digest

    async def prepare(self) -> dict[str, Any]:
        snapshot = await self.inner.prepare()
        self.seal = await self._hash()
        return snapshot

    async def check(self) -> dict[str, Any]:
        if self.seal is None:
            raise SnapshotModified("no sealed snapshot to check against")
        if await self._hash() != self.seal:
            raise SnapshotModified("the snapshot was modified after it was taken")
        await self.inner.install()  # the verifier's own code, uploaded again
        return await self.inner.check()


def mentions_verifier(stream_log: str | None) -> bool:
    """Whether anything in the baseline's output names the verifier's folders."""
    text = stream_log or ""
    return CONTAINER_WORK in text or CONTAINER_SOURCE in text
