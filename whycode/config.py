"""Configuration: everything comes from the environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_database: str = "neo4j"

    github_token: str | None = None
    github_repo: str = "pallets/flask"  # owner/name, the demo target

    max_commits: int = 1200
    max_prs: int = 150
    max_issues: int = 150

    confidence_threshold: float = 0.45

    data_dir: str = field(
        default_factory=lambda: os.path.join(
            os.path.expanduser("~"), ".whycode"
        )
    )

    # Optional LLM verbalizer (OpenAI-compatible HTTP endpoint). Unset = templates.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
            neo4j_password=os.environ.get("NEO4J_PASSWORD", "password"),
            neo4j_database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            github_token=os.environ.get("GITHUB_TOKEN"),
            github_repo=os.environ.get("GITHUB_REPO", "pallets/flask"),
            max_commits=_int("WHYCODE_MAX_COMMITS", 1200),
            max_prs=_int("WHYCODE_MAX_PRS", 150),
            max_issues=_int("WHYCODE_MAX_ISSUES", 150),
            confidence_threshold=_float("WHYCODE_CONFIDENCE_THRESHOLD", 0.45),
            data_dir=os.environ.get(
                "WHYCODE_DATA_DIR",
                os.path.join(os.path.expanduser("~"), ".whycode"),
            ),
            llm_base_url=os.environ.get("LLM_BASE_URL"),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
        )

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/")[1]

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.github_repo}.git"

    @property
    def clone_dir(self) -> str:
        return os.path.join(
            self.data_dir, "repos", self.github_repo.replace("/", "__")
        )

    @property
    def rawctx_path(self) -> str:
        return os.path.join(
            self.data_dir, "rawctx",
            self.github_repo.replace("/", "__") + ".sqlite",
        )
