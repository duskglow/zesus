"""Exception hierarchy shared by all components."""


class ZfsRecoverError(Exception):
    """Base class for all errors raised by this package."""


class SourceWriteAttempt(ZfsRecoverError):
    """Raised when anything in the process tries to modify a protected source."""


class SourceChanged(ZfsRecoverError):
    """Raised when the source's size or mtime changed while we were reading it."""


class CorruptStructure(ZfsRecoverError):
    """An on-disk structure failed validation (bad magic, impossible field values, ...)."""


class ChecksumMismatch(CorruptStructure):
    """A block's computed checksum does not match the one recorded in its block pointer."""

    def __init__(self, message: str, expected: tuple[int, ...] | None = None,
                 actual: tuple[int, ...] | None = None) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class DecompressionError(CorruptStructure):
    """A compressed block could not be decompressed to its logical size."""


class Unsupported(ZfsRecoverError):
    """A feature exists on disk that this version cannot interpret (e.g. RAIDZ, encryption)."""
