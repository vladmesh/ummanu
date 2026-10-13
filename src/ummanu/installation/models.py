"""Shared install errors, results and recovery progress filename."""

from __future__ import annotations

from dataclasses import dataclass, field


class InstallError(RuntimeError):
    """A controlled install/recovery refusal."""



@dataclass
class InstallStep:
    name: str
    status: str
    detail: str = ""



@dataclass(frozen=True)
class ProjectProvisionResult:
    project_id: str
    target: str
    transport: str
    outcome: str
    code: str
    reason: str
    retryable: bool



@dataclass
class InstallResult:
    steps: list[InstallStep] = field(default_factory=list)
    projects: list[ProjectProvisionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def status(self) -> str:
        if any(step.status == "failed" for step in self.steps):
            return "failed"
        if any(step.status == "degraded" for step in self.steps) or any(
            project.outcome == "failed" for project in self.projects
        ):
            return "degraded"
        return "ok"

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(InstallStep(name, status, detail))

    def render(self) -> str:
        lines = ["ummanu install"]
        for step in self.steps:
            suffix = f": {step.detail}" if step.detail else ""
            lines.append(f"  {step.status:9} {step.name}{suffix}")
        if self.projects:
            lines.append("projects:")
            for project in self.projects:
                lines.append(
                    f"  {project.outcome:9} {project.project_id} "
                    f"target={project.target} transport={project.transport} "
                    f"code={project.code} retryable={'yes' if project.retryable else 'no'}: "
                    f"{project.reason}"
                )
        lines.append("status: " + self.status)
        return "\n".join(lines)



@dataclass(frozen=True)
class PipelineStateMaterialization:
    records: int
    changed: bool



RECOVERY_PROGRESS_FILE = "recovery-progress.json"
