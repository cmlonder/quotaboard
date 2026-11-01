# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
A solo developer who runs many AI coding agents at once (Claude Code sessions, Multica agents, Antigravity, Codex) on one machine and keeps running into quota limits. Later possibly other AI-heavy developers with the same setup. They open the page many times a day, mostly to answer one question: will a quota run out, and where is there still room.

## Product Purpose
One local page that shows what is left of every LLM quota on the machine and when it resets, who and what spent it, and plain-rule advice on waste, with a few confirmed actions to fix it. Success: the user sees at a glance whether a limit will hit before it resets and what to do about it, without opening several apps.

## Positioning
It reads what is already on the machine (transcripts, CLIs, local auth) and joins them: Claude Code sessions are tied to the agent run and issue that produced them, and quotas from several providers sit side by side. No account, no cloud, no key of its own.

## Operating Context
Runs from a terminal command, served on 127.0.0.1, viewed in a desktop browser next to the agents it watches. Refreshes on its own every 30 seconds; sources refresh every 1–5 minutes.

## Capabilities and Constraints
- Python standard library server plus one static HTML page; no build step, no external CDN or assets.
- Local only; actions need a page token and an explicit confirmation; there is no automatic brake by design.
- Claude's real plan percentages are not readable by scripts; the page estimates the 5-hour window and lets the user type the app's numbers in.
- English and Turkish UI; default follows the browser language, with a switch that is remembered.

## Evidence on Hand
Live data on the user's machine only. No screenshots, testimonials or usage numbers to publish.

## Product Principles
- Answer "will I run out, and where is there room" first; analysis second.
- Every number says where it comes from; every estimate is labelled as one.
- Advice is a plain rule with its reason, never a black box.
- Actions are reversible and always confirmed.
