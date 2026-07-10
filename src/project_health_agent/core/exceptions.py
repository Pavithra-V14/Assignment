"""
Typed exception hierarchy.

Enterprise callers (schedulers, alerting, CI) need to distinguish "a data
source was unreachable, retry later" from "a spreadsheet is malformed, page
a human" from "the LLM provider is misconfigured." Catching bare Exception
everywhere makes that impossible; catching these lets the CLI layer set
correct process exit codes and log levels per failure class.
"""


class ProjectHealthAgentError(Exception):
    """Base class for all agent-raised errors."""


class DataSourceError(ProjectHealthAgentError):
    """Raised when a configured data source cannot be read at all."""


class DriveAuthError(DataSourceError):
    """Google Drive credentials are missing, invalid, or lack folder access."""


class DriveSyncError(DataSourceError):
    """A Drive API call failed after retries were exhausted."""


class PlanParseError(ProjectHealthAgentError):
    """A project plan workbook could not be parsed into the expected schema."""


class ScoringError(ProjectHealthAgentError):
    """The deterministic scoring engine could not produce a composite score."""


class LLMProviderError(ProjectHealthAgentError):
    """The configured LLM provider returned an unusable response."""


class ConfigurationError(ProjectHealthAgentError):
    """Required settings are missing or mutually inconsistent."""
