# Rocketlane automation: overdue task escalation

This is the one part of the assignment that lives in Rocketlane's UI rather than in code.
Two rules on the same trigger. Do them in your trial account before recording the video,
then show them on screen.

## Where
Avatar (bottom left) > Settings > Advanced > Automations > New automation

Rocketlane also lets automations be attached at template level. If you set up a project
template, add these two rules to the template so every project created from it inherits them.

## Rule 1: notify the Project Manager at one day overdue
- Name: `Overdue 1 day -> notify PM`
- Trigger type: Task
- Trigger: **Task becomes overdue**
- Condition: overdue by **1** day  (the trigger exposes "overdue by N days" as a condition)
- Action: **Notify someone** -> recipient: Project Manager
  (If the recipient list offers "Project owner" and a role picker rather than "Project Manager",
  pick the project role the CS team uses for the day-to-day PM. Rocketlane's help center uses the
  wording "project owner" in its example, so screenshot whatever the dropdown actually says.)
- Message: `{{task.name}} on {{project.name}} is 1 day overdue. Assignee: {{task.assignee}}.`

## Rule 2: notify the Project Owner at four days overdue
- Name: `Overdue 4 days -> notify Owner`
- Trigger: **Task becomes overdue**
- Condition: overdue by **4** days
- Action: **Notify someone** -> recipient: Project Owner
- Optional second action: **Update task field** -> mark "At risk". Priya's team wanted blocked
  items surfaced; at-risk is the visible flag in Rocketlane's project view.
- Message: `{{task.name}} on {{project.name}} is 4 days overdue and has not been escalated. Owner attention needed.`

## How to show it in the video
1. Open Settings > Advanced > Automations and show both rules enabled.
2. Open the project the agent created. Pick any task, edit its due date to yesterday, save.
3. Show the notification arriving (in-app bell, or the email if you enabled it).
   Note for the panel: the trigger evaluates on Rocketlane's schedule, so if it does not fire
   instantly on camera, say so and show the rule definition plus the audit trail instead. That is
   the honest version and reviewers respect it.

## Why this is an automation and not an agent
Priya said escalation "falls through the cracks a lot". Anything that depends on a process
remembering to run is the same failure mode as a person remembering to run it. Rocketlane's
automation fires on the platform's clock, per task, with no external dependency. An agent that
polled tasks every hour would be a worse version of the same thing with one more component to
break.
