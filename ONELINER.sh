#!/usr/bin/env bash
# Copy-paste ONE command for your team (edit GIT_REPO_URL first).
#
# Option A — everyone clones your dotfiles repo:
GIT_REPO_URL='git@bitbucket.org:YOUR_TEAM/YOUR_DOTFILES.git'
git clone --depth 1 "${GIT_REPO_URL}" /tmp/jira-cli-install && bash /tmp/jira-cli-install/jira-cli/setup.sh
#
# Option B — you already have this repo on disk:
# bash ~/dotfiles/jira-cli/setup.sh
