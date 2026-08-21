from dataclasses import dataclass
from pathlib import Path


@dataclass
class File:
    target_id: str
    local_path: Path
    target_path: Path

    @property
    def relative_path(self) -> Path:
        return self.local_path.relative_to(self.target_path)
