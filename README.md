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
