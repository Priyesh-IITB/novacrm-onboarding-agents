"""Draws docs/workflow.png: the proposed agentic workflow as a flowchart.
Pure matplotlib so it builds anywhere. Run: python docs/make_diagram.py"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon
import os

INK, MUTED = "#15191C", "#5E6560"
AGENT1, AGENT2, PLAT, STOP, OK = "#E4EFED", "#E6ECF4", "#F6EDDD", "#F3E3E1", "#E3F0E7"
FS = 7.4

fig, ax = plt.subplots(figsize=(11, 10), dpi=200)
ax.set_xlim(0, 110); ax.set_ylim(6, 103); ax.axis("off")


def box(x, y, w, h, text, fill, bold=False, fs=FS):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.8",
                                fc=fill, ec=INK, lw=0.9))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, color=INK, fontweight="bold" if bold else "normal",
            linespacing=1.25)


def diamond(x, y, w, h, text, fs=FS):
    ax.add_patch(Polygon([(x, y + h / 2), (x + w / 2, y), (x, y - h / 2), (x - w / 2, y)], closed=True, fc="white", ec=INK, lw=0.9))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, color=INK, linespacing=1.2)


def arrow(x1, y1, x2, y2, label="", lx=0, ly=0, color=INK):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle="-|>", color=color, lw=0.9, shrinkA=0, shrinkB=0))
    if label:
        ax.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, fontsize=6.8, color=MUTED, ha="center", va="center",
                bbox=dict(fc="white", ec="none", pad=1.0))


def lane(y_top, y_bot, label, fill):
    ax.add_patch(FancyBboxPatch((1, y_bot), 108, y_top - y_bot, boxstyle="round,pad=0,rounding_size=1.2", fc=fill, ec="none", alpha=0.45))
    ax.text(2.4, y_top - 1.6, label, fontsize=7.6, color=MUTED, ha="left", va="top", fontweight="bold")


lane(102, 45.5, "AGENT 1: INTAKE AND ROUTING", AGENT1)
lane(44.5, 33.5, "AGENT 2: COMMUNICATION", AGENT2)
lane(32.5, 13.5, "ROCKETLANE PLATFORM AUTOMATION (configured in the product, not an agent)", PLAT)

X = 32
box(X, 94, 42, 5.4, "Deal email arrives in the CS inbox\n(polled by message id; own replies and auto-mail ignored)", "white")
box(X, 86, 38, 5.4, "Parse: labelled lines first, an LLM only for free text;\nboth outputs go through the same validation", "white")
diamond(X, 77, 24, 8, "All four fields\npresent and valid?")
diamond(X, 67, 28, 8, "Existing onboarding project\nfor this customer?")
box(X, 57.5, 36, 5.4, "Voice call to the AE: Enterprise or Growth?\n(up to 2 attempts, with a delay between)", "white")
diamond(72, 57.5, 22, 8, "Unambiguous\nanswer?")
box(72, 49, 36, 5.2, "Create Rocketlane project: tier template,\n4 phases, 15 tasks, dates from the timeline", OK, bold=True)

arrow(X, 91.3, X, 88.7)
arrow(X, 83.3, X, 81)
arrow(X, 73, X, 71, "yes", lx=3.0)
arrow(X, 63, X, 60.2, "no", lx=2.6)
arrow(50, 57.5, 61, 57.5)
arrow(72, 53.5, 72, 51.6, "yes", lx=3.0)

# right-hand exits (agent 1)
box(94, 77, 28, 6.8, "Clarification email back to the AE\nlisting exactly what is missing,\nin the format it wants. Nothing created.", STOP)
arrow(44, 77, 80, 77, "no / malformed", ly=1.6)
box(94, 67, 28, 6.8, "Escalate to a person, with the\nexisting project id. No call placed,\nno second project.", STOP)
arrow(46, 67, 80, 67, "yes, or the lookup failed", ly=1.6)
box(97, 57.5, 22, 6.8, "Retry once. Still unclear:\nescalate with both\ntranscripts. Never assume.", STOP)
arrow(83, 57.5, 86, 57.5, "no", ly=1.6)

# rocketlane failure exit (agent 1, same row as create)
box(26, 49, 36, 5.2, "API down: retries with backoff, then escalate (high).\nPartial create: escalate with the project id.", STOP)
arrow(54, 49, 44, 49)

# agent 2
box(72, 39, 46, 5.2, "Create the shared Slack channel: name, topic and welcome\nwritten for the tier; invite the AE and the customer contact", OK, bold=True)
arrow(72, 46.4, 72, 41.6)
box(24, 39, 32, 5.2, "Slack fails after the project exists:\nescalate with the project id (medium)", STOP)
arrow(49, 39, 40, 39)

# platform lane
box(30, 24, 42, 5.6, "Task overdue by 1 day: notify the Project Manager\nTask overdue by 4 days: notify the Project Owner", "white", bold=True)
box(78, 24, 34, 5.6, "Fires on the platform's clock, per task,\nwhether or not anyone remembers to check", "white")
arrow(72, 36.4, 72, 26.8, color=MUTED)
ax.text(55, 16.6, "Every step above writes an audit entry: timestamp, agent, inputs, outputs and the rationale for the decision. Secrets are redacted before they hit disk.",
        fontsize=7.2, color=MUTED, ha="center", style="italic")
ax.text(55, 9.5, "Figure 1. Proposed workflow. Red boxes are the exits: the system asks or escalates; it never guesses a field or a tier.",
        fontsize=7.6, color=INK, ha="center")

out = os.path.join(os.path.dirname(__file__), "workflow.png")
fig.savefig(out, bbox_inches="tight", facecolor="white")
print("wrote", out)
