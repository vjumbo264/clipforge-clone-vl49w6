# ClipForge Bot — Setup & User Guide

Canonical Telegraph copy: https://telegra.ph/ClipForge-Bot--Setup--User-Guide-08-27

This file records the published guide so future sessions can update and re-post it.
Content is authored against the live bot UI (bot/src/views.js, commands/*, runtime.js).

## bug-29 (this sweep)
The previous page (…Complete-User-Guide-08-27-2) was replaced:
- **Added** full onboarding: (1) create a GitHub account, (2) set up the repository via
  the bot's "Create private Shadow Clone" / "Connect existing clone" flow, (3) create a
  classic GitHub PAT with the minimum scopes `repo` + `workflow`, including an ASCII
  diagram of the flow.
- **Removed** all embedded images/screenshots (they were placehold.co mockups, not done
  properly). The page is now text-only with ASCII art where a visual helps.

## Edit access (keep secret — do not commit elsewhere / do not share publicly)
- access_token (page edit): 5e479bb63ae1fda05052c98d049124ea1c5052556c8bd42f23d896695527
- Edit endpoint: https://api.telegra.ph/editPage (POST access_token, path, title, content)

## Current page outline
1. Create a GitHub account
2. Set up the repository (the clone)
3. Create a GitHub PAT (repo + workflow)  [ASCII diagram]
4. Starting a video — /new
5. Stage A then Stage B
6. When the task finishes
7. Commands
Good to know
