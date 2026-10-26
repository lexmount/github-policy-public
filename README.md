# Public OCR dispatch workflow

The OCR dispatch entry point for selected public repositories in the organization. Workflow source: `.github/workflows/codex-code-review.yml` on the `main` branch.

## Administrator setup

1. In the organization settings, go to **Settings → Rules → Rulesets** and create a **Branch ruleset**, for example, `OCR public repositories`.
2. Under **Target repositories**, choose **Only selected repositories** and initially select `lexmount/lexbench-webfetch`. Set **Target branches** to `main`.
3. Enable **Require workflows to pass before merging**. Select source repository `lexmount/github-policy-public`, branch `main`, and file `.github/workflows/codex-code-review.yml`.
4. Under **Settings → Secrets and variables → Actions**, edit the existing organization secret `OCR_CI_TOKEN`. Choose **Selected repositories**, retain all existing repository access, and add `lexbench-webfetch`. Do not commit the token to this repository. A secret scoped to **Private repositories** does not cover public repositories.
5. Exclude this public repository from the previous OCR Ruleset that references the private `github-policy` workflow. Leave other protection rules unchanged.
6. After completing the configuration, set the new ruleset to **Active** and verify it with a new PR or commit.

The workflow exits once the backend confirms that the task has been durably accepted. Submission and polling take no more than 90 seconds in total, excluding runner queue and startup time. The job has a hard timeout of 3 minutes. n8n decides whether to route the task through the Agent or the default OCR path. The final `ocr-review` status is reported separately. It is currently advisory and is not configured as required. To require a successful review before merging, add a separate required status check.

## Security and limitations

The workflow uses `pull_request_target` and reads only the PR identifiers from the GitHub event. It does not check out, import, or execute PR code, making it suitable for PRs from external forks. It sets `permissions: {}`. The dispatch token is injected only through an organization secret accessible to the target repository, and is not printed in execution logs. The workflow embeds a Python standard-library client and requires no third-party Actions or runtime dependency downloads. Merge queues and `merge_group` are not supported yet.

Only the dispatch client, generator, and tests are public. This repository does not contain OCR service code or credentials.

## Maintenance and verification

```sh
python3 ocr/render_workflow.py
python3 -m unittest discover -s ocr -p 'test_*.py'
```

Regenerate the YAML after editing `ocr/ci_client.py`. The tests verify that the embedded client stays in sync.

## Quick rollback

An administrator can set the new `OCR public repositories` Ruleset to **Disabled** to remove this required-workflow constraint. Keep other rules and existing secret access unchanged. Do not restore the public repository's reference to the private policy workflow. Disabling the ruleset does not stop OCR tasks already accepted by the backend.
