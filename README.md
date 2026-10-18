# Github Manager

A simple project built on top of the `gh` cli utility that allows you to effectively manage large numbers of projects.

## Requirements

- Python 3. Tested with 3.9.
- The [`gh` cli](https://cli.github.com/)

## Configuration

The script uses a configuration file located at `~/.ghm/repos.json`. This should be a list of the repos to search.

Ex:

```json
[
    "org/repo1",
    "org/repo2",
    "other-org/repo3"
] 
```

## Usage

If using `direnv`, add `.envrc` and `direnv allow`.

```
use_pyenv
layout python python3
export PYTHONPATH=.
```

or `export PYTHONPATH=.` and ensure that you have `python` or `python3` available.

Install required libraries:

```bash
pip install -r requirements.txt
```

To run:

```bash
python scripts/manage.py --help
```

## Functionality

- List configured repos
- Checking PRs across a number of projects
- Approve similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Merging similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Re-running failed tests from actions triggered by similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Open your browser to a particular PR in a particular repo
- Run actions across a number of projects (i.e. `Unit tests` across 25 projects)
- Bulk enable and disable workflows across projects
- List draft releases across a number of projects
- Publish a draft release across a number of projects
- Create and update repository secrets (Actions, Agents, Codespaces, Dependabot)

## Repository secrets

`repos secrets create` and `repos secrets update` write a repository secret. The
value is read from STDIN when it is piped in, or from the terminal without
echoing when it is not:

```bash
python scripts/manage.py repos secrets update --repo org/repo MY_SECRET < my-secret.txt
python scripts/manage.py repos secrets update --repo org/repo MY_SECRET
```

`create` refuses to overwrite a secret that already exists, and `update` refuses
to touch one that does not. Both default to an Actions secret; pass `--type` to
target `agents`, `codespaces`, or `dependabot` instead. These commands only
handle repository secrets. Environment secrets and Actions variables are out of
scope.

`repos secrets list` shows the secrets in a repo, oldest first, with when each
was created and last updated:

```bash
python scripts/manage.py repos secrets list --repo org/repo
python scripts/manage.py repos secrets list --repo org/repo --type actions
```

It lists every type by default. GitHub does not store an expiration date for
secrets, so the table shows timestamps instead.
