import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "choose_registry.sh"


def choose(tmp_path, **env):
    out = tmp_path / "out"
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DOCKERHUB_")}
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**clean, "GITHUB_OUTPUT": str(out), "GITHUB_REPOSITORY": "DwightMulcahy/My-App", **env},
        capture_output=True, text=True, check=True,
    )
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    return outputs, r.stdout


@pytest.mark.parametrize(
    "env, missing",
    [
        ({}, "DOCKERHUB_USERNAME variable and DOCKERHUB_TOKEN secret"),
        ({"DOCKERHUB_USERNAME": "dockeruser", "DOCKERHUB_TOKEN_SET": "false"}, "DOCKERHUB_TOKEN secret"),
        ({"DOCKERHUB_TOKEN_SET": "true"}, "DOCKERHUB_USERNAME variable"),
    ],
)
def test_falls_back_to_ghcr_without_docker_hub_credentials(tmp_path, env, missing):
    outputs, stdout = choose(tmp_path, **env)
    assert outputs == {"registry": "ghcr.io", "image": "ghcr.io/dwightmulcahy/my-app"}
    assert "::notice title=Publishing to GHCR::" in stdout
    assert f"missing {missing}" in stdout


def test_uses_docker_hub_when_configured(tmp_path):
    outputs, stdout = choose(tmp_path, DOCKERHUB_USERNAME="DockerUser", DOCKERHUB_TOKEN_SET="true")
    assert outputs == {"registry": "docker.io", "image": "docker.io/dockeruser/my-app"}
    assert "::notice" not in stdout


def test_docker_hub_repo_override(tmp_path):
    outputs, _ = choose(
        tmp_path, DOCKERHUB_USERNAME="dockeruser", DOCKERHUB_REPO="Org/Custom", DOCKERHUB_TOKEN_SET="true"
    )
    assert outputs["image"] == "docker.io/org/custom"


def test_release_workflow_uses_chosen_registry():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "bash scripts/choose_registry.sh" in wf
    assert "DOCKERHUB_TOKEN_SET: ${{ secrets.DOCKERHUB_TOKEN != '' }}" in wf
    assert "registry: ${{ needs.verify.outputs.registry }}" in wf
    assert "packages: write" in wf
    assert "env.IMAGE" not in wf
