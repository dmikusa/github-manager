# Github Manager

A simple project built on top of the `gh` cli utility that allows you to effectively manage large numbers of projects.

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

## Functionality

- List configured repos
- Checking PRs across a number of projects
- Approve similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Merging similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Re-running failed tests from actions triggered by similar PRs across a number of projects (i.e. `Bump abc to x.y.z` across 25 projects)
- Open your browser to a particular PR in a particular repo
- Run actions across a number of projects (i.e. `Unit tests` across 25 projects)
- List draft releases across a number of projects
- Publish a draft release across a number of projects
