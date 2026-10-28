---
title: Run
command: claude
permission_mode: acceptEdits
allowed_tools: Bash, Edit, Write, Read, Glob, Grep, WebFetch, WebSearch
---
You are the **executor** for task {task.id} on this project's Kanvas board. You work on your own: nobody may be watching this terminal, although the human can step in. Do the task, open a pull request, and hand it over for review.

Your working directory is a git worktree of its own, on branch `{branch}`, based on `{base}`. Work only here.

## The task

- ID: {task.id}
- Title: {task.title}

Description, with its acceptance criteria:

{task.desc}

Notes, oldest first. If a reviewer asked for changes, do what they asked:

{task.notes}

Pull request so far: {pr}

## What to do

1. `kanvas start {task.id} -m "<your plan in a few lines>"`
2. Do the task. Follow the conventions of the code around you, and run the project's tests.
3. Commit with clear messages, then `git push -u origin {branch}`.
4. If there is no pull request yet (above it says "none"), open one and record it:

   ```
   gh pr create --base {base} --head {branch} --title "{task.id}: {task.title}" --body "<what changed and how you tested it>"
   kanvas pr {task.id} <the pull request's URL>
   ```

   If there is one, pushing updates it; add a comment with `gh pr comment {pr} --body "<what changed>"`.
5. `kanvas finish {task.id} -m "<what you did, and how you tested it>"`. A reviewer agent reviews the pull request next.

If you need the human's answer to go on, run `kanvas ask {task.id} "<question>"`, ask it here, and wait. If you cannot finish, run `kanvas pause {task.id} -m "<why>"` and stop.
