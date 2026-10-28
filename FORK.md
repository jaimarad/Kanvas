# Kanvas with agents (jaimarad/Kanvas)

This fork of [XMihura/Kanvas](https://github.com/XMihura/Kanvas) runs the agents from the board, so you never need a terminal of your own. Everything in the upstream [README](README.md) still applies; this page covers what the fork adds.

## What it adds

Open a task and use the **Agents** section of its panel:

| Button | Agent | What happens |
|--------|-------|--------------|
| **Validate** | validator | Claude opens in a terminal at the bottom of the page, reads the code and the task, and works out with you whether it is doable and what exactly it should do. When you agree, it saves the description, with acceptance criteria, and moves the task to To do. |
| **Run** | executor | Claude works on its own in a git worktree on branch `kanvas/<id>`, commits, pushes, opens a pull request, and moves the task to Review. You can watch, and type into its terminal, at any time. |
| **Review** | reviewer | Starts by itself when the executor finishes. Claude reviews the pull request against the task, comments on it, and gives a verdict: **approve** (the task is done) or **changes** (the task goes back to To do, marked to be validated again, with the feedback in its notes). |

**On approval**, in the same section, chooses what happens to the pull request when the reviewer approves: **I merge** (the default: it waits for you on GitHub) or **Auto-merge** (the reviewer squash-merges it). A run after a request for changes continues the same branch and pull request.

Cards show what is going on: *Validating*, *Running* or *Reviewing* while an agent works, *Revalidate* when the reviewer sent a task back, and *PR* when a pull request waits. **Terminal** opens a plain shell in the project, too. Sessions keep running when you close the page; open them again from the task.

## Each agent's prompt

The first time an agent runs in a project, Kanvas copies its prompts to `.kanvas/agents/validator.md`, `executor.md` and `reviewer.md`. Edit them to suit the project and commit them; Kanvas never overwrites them. The defaults are in [`agents/`](agents/).

```markdown
---
title: Review
command: claude
model: opus
permission_mode: default
allowed_tools: Bash, Read, Glob, Grep
disallowed_tools: Edit, Write
---
You are the reviewer for task {task.id}. ...
```

| Setting | Meaning |
|---------|---------|
| `title` | The name on the session |
| `command` | The program to run (default `claude`); the prompt is its first argument |
| `model`, `permission_mode`, `allowed_tools`, `disallowed_tools` | Passed to Claude Code as `--model`, `--permission-mode`, `--allowedTools`, `--disallowedTools` |

In the prompt, Kanvas fills in `{task.id}`, `{task.title}`, `{task.desc}`, `{task.notes}`, `{task.group}`, `{task.state}`, `{branch}`, `{base}`, `{pr}` and `{merge}`.

Project settings live in `.kanvas/config.json`:

```json
{ "merge": "manual", "auto_review": true, "base": "main" }
```

`merge` is the default for new tasks (`manual` or `auto`), `auto_review` starts the reviewer when the executor finishes, and `base`, optional, is the branch pull requests target (by default the remote's default branch).

## The rules the agents follow

The agents change the board through the `kanvas` command like any agent, with three commands of their own that only the matching agent, started by Kanvas, may use:

- `kanvas validate <ID> [--stdin | --desc TEXT] -m "summary"`: saves the agreed description, approves a proposal, and clears *Revalidate*.
- `kanvas pr <ID> <url>`: records the task's pull request.
- `kanvas verdict <ID> approve|changes -m "summary"`: marks the task done (merging in Auto-merge mode) or sends it back.

The executor cannot run a task that waits to be validated again; **Skip** in the panel lets it run anyway. You can still do everything by hand, as upstream.

Agents need a git repository whose `origin` is on GitHub, `gh` logged in, and Claude Code logged in. Worktrees live in `<repo>/.kanvas/worktrees/`, which Kanvas adds to `.git/info/exclude`; after an auto-merge the worktree and local branch are removed.

## Run it locally (Docker)

The agent features need Linux, so they run in Docker; on Windows and macOS without Docker, Kanvas works as upstream.

```bash
docker compose up -d --build                   # the board at http://127.0.0.1:8865/
docker compose exec kanvas claude              # log in to Claude Code once, then /exit
docker compose exec kanvas gh auth login       # log in to GitHub once
docker compose exec kanvas gh auth setup-git   # lets git push with that login
docker compose exec kanvas git clone https://github.com/you/project /workspace/project
docker compose exec -w /workspace/project kanvas kanvas init
docker compose exec -w /workspace/project kanvas kanvas serve
```

The first time Claude Code runs in a project it asks whether you trust the folder: answer in the validator's terminal. Projects live in the `workspace` volume, logins in the `auth` volume.

## Deploy on a VPS

The board is a terminal into the server, so it must never be public. The VPS setup serves it only on your [Tailscale](https://tailscale.com) network, over HTTPS, and publishes no port.

1. On the VPS, install Docker and clone this fork.
2. In the Tailscale admin console, enable MagicDNS and HTTPS certificates, and create an auth key.
3. `cp deploy/.env.example deploy/.env` and fill in `TS_AUTHKEY`, and the name and email for the agents' commits.
4. `docker compose -f deploy/docker-compose.yml up -d --build`
5. Log in, as above, with `docker compose -f deploy/docker-compose.yml exec kanvas ...`, and clone and open your projects the same way.
6. Open `https://kanvas.<your tailnet>.ts.net/` from any device on your tailnet.

Docker restarts both containers after a reboot (enable Docker at boot: `systemctl enable docker`). To update: `git pull`, then `docker compose -f deploy/docker-compose.yml up -d --build`.

## Developing the fork

See the fork's section in [AGENTS.md](AGENTS.md). Run the tests in Docker: `docker compose exec kanvas sh -c 'cd /opt/kanvas && python -m unittest discover -s tests'`.
