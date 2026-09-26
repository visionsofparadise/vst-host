import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

function runGh(arguments_) {
  return execFileSync("gh", arguments_, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function repositoryOf(value) {
  if (
    !/^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+$/u.test(value ?? "") ||
    value.split("/").some((part) => part === "." || part === "..")
  )
    throw new Error("A GitHub repository is required");
  return value;
}

function versionOf(tag) {
  if (!/^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u.test(tag ?? ""))
    throw new Error("A stable release tag is required");
  return tag.slice(1).split(".").map(BigInt);
}

function olderThan(left, right) {
  for (let index = 0; index < 3; index++) {
    if (left[index] < right[index]) return true;
    if (left[index] > right[index]) return false;
  }
  return false;
}

function releasesOf(repository, run) {
  const releases = [];
  for (let page = 1; ; page++) {
    const listed = JSON.parse(
      run(["api", `repos/${repository}/releases?per_page=100&page=${page}`]),
    );
    if (!Array.isArray(listed)) throw new Error("Invalid release listing");
    releases.push(...listed);
    if (listed.length < 100) return releases;
  }
}

function pruneOlderReleases({ repository, tag, run = runGh }) {
  repositoryOf(repository);
  const currentVersion = versionOf(tag);
  const current = JSON.parse(
    run(["api", `repos/${repository}/releases/tags/${tag}`]),
  );
  if (current.tag_name !== tag || current.draft || current.prerelease)
    throw new Error(
      "The current stable release must be published before pruning",
    );
  const older = releasesOf(repository, run).filter((release) => {
    if (
      release.draft ||
      release.prerelease ||
      !/^v\d+\.\d+\.\d+$/u.test(release.tag_name ?? "")
    )
      return false;
    return olderThan(versionOf(release.tag_name), currentVersion);
  });
  for (const release of older) {
    if (!Number.isSafeInteger(release.id) || release.id <= 0)
      throw new Error("Invalid older release ID");
  }
  for (const release of older)
    run([
      "api",
      "--method",
      "DELETE",
      `repos/${repository}/releases/${release.id}`,
    ]);
  return older.length;
}

function deleteRunArtifacts({ repository, runId, run = runGh }) {
  repositoryOf(repository);
  if (!/^[1-9]\d*$/u.test(String(runId ?? "")))
    throw new Error("A workflow run ID is required");
  const artifacts = [];
  for (let page = 1; ; page++) {
    const response = JSON.parse(
      run([
        "api",
        `repos/${repository}/actions/runs/${runId}/artifacts?per_page=100&page=${page}`,
      ]),
    );
    if (
      !Array.isArray(response.artifacts) ||
      !Number.isInteger(response.total_count)
    )
      throw new Error("Invalid workflow artifact listing");
    artifacts.push(...response.artifacts);
    if (artifacts.length >= response.total_count) break;
    if (response.artifacts.length === 0)
      throw new Error("Incomplete workflow artifact listing");
  }
  for (const artifact of artifacts) {
    if (
      !Number.isSafeInteger(artifact.id) ||
      artifact.id <= 0 ||
      (artifact.workflow_run?.id !== undefined &&
        String(artifact.workflow_run.id) !== String(runId))
    )
      throw new Error("Artifact does not belong to the requested workflow run");
  }
  for (const artifact of artifacts)
    run([
      "api",
      "--method",
      "DELETE",
      `repos/${repository}/actions/artifacts/${artifact.id}`,
    ]);
  return artifacts.length;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const [command, tag] = process.argv.slice(2);
  if (command === "releases") {
    if (/^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u.test(tag ?? ""))
      console.log(
        `Deleted ${pruneOlderReleases({ repository: process.env.GITHUB_REPOSITORY, tag })} older stable releases`,
      );
    else console.log(`Skipped release pruning for non-stable tag ${tag}`);
  } else if (command === "artifacts")
    console.log(
      `Deleted ${deleteRunArtifacts({ repository: process.env.GITHUB_REPOSITORY, runId: process.env.GITHUB_RUN_ID })} temporary workflow artifacts`,
    );
  else
    throw new Error(
      "Usage: node releaseRetention.mjs releases <tag>|artifacts",
    );
}
