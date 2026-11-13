"""Focused regression tests for secureio primitives."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ohmyllm.secureio import atomic_write


class TestAtomicWriteDoubleClose:
    """Prove a failing commit_check does not double-close the fd.

    After os.fdopen(fd, closefd=True) takes ownership and the with-block exits
    normally, a subsequent commit_check failure must not attempt os.close(fd)
    again — that would corrupt the fd table or raise EBADF.

    Note: fdopen's internal close uses the C-level close(2) syscall directly,
    bypassing Python's os.close(). So we track os.close calls to prove the
    except-handler does NOT issue a redundant Python-level close after fdopen
    already took ownership.
    """

    def test_commit_check_failure_does_not_double_close(self, tmp_path: Path):
        """fdopen took ownership -> except handler must NOT call os.close."""
        target = tmp_path / "out.bin"
        closed_fds: list[int] = []
        real_close = os.close

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with patch("os.close", tracking_close):
            with pytest.raises(RuntimeError, match="commit rejected"):
                atomic_write(
                    target,
                    b"payload",
                    commit_check=lambda: (_ for _ in ()).throw(RuntimeError("commit rejected")),
                )

        # fdopen(closefd=True) closed the fd internally via C close(2).
        # The except handler must NOT have issued a redundant os.close(fd).
        assert len(closed_fds) == 0, (
            f"expected 0 os.close calls (fdopen owns fd), got {len(closed_fds)}: {closed_fds}"
        )
        # Temp file must be cleaned up.
        assert not target.exists()
        leftovers = list(tmp_path.glob(".*tmp"))
        assert not leftovers, f"temp files not cleaned: {leftovers}"

    def test_cancel_before_fdopen_closes_fd_once(self, tmp_path: Path):
        """If an error occurs before fdopen takes ownership, fd is closed once."""
        target = tmp_path / "out.bin"
        closed_fds: list[int] = []
        real_close = os.close

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        # Simulate failure in fchmod (before fdopen) on POSIX only
        if os.name == "posix":
            with patch("os.close", tracking_close):
                with patch("os.fchmod", side_effect=OSError("simulated fchmod fail")):
                    with pytest.raises(OSError, match="simulated fchmod fail"):
                        atomic_write(target, b"payload")

            # fdopen never took ownership, so the except handler must close fd.
            assert len(closed_fds) == 1
            assert not target.exists()
        else:
            pytest.skip("fchmod is POSIX-only")

    def test_successful_write_no_redundant_close(self, tmp_path: Path):
        """Normal success path: fdopen closes fd internally, no os.close call."""
        target = tmp_path / "out.bin"
        closed_fds: list[int] = []
        real_close = os.close

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with patch("os.close", tracking_close):
            result = atomic_write(target, b"hello world")

        assert result == target
        assert target.read_bytes() == b"hello world"
        # fdopen's C-level close handled it; no Python os.close needed.
        assert len(closed_fds) == 0
