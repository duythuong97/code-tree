"""BaseExtractor ABC — implement this to add a new language or format."""

from __future__ import annotations

from abc import ABC, abstractmethod

from contract.entities import ExtractionContext, ExtractionResult


class BaseExtractor(ABC):
    """Stateless parser. Valid-empty returns empty; parser failures raise."""

    @abstractmethod
    def can_handle(self, file_path: str, text: str) -> bool:
        """Return whether decoded content is valid for this handler."""

    @abstractmethod
    def extract(
        self,
        file_path: str,
        text: str,
        context: ExtractionContext,
    ) -> ExtractionResult:
        """Parse one file without persistence side effects."""
