from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from nicegui import events
from nicegui import run as nicegui_run
from nicegui import ui

from dqtool.models.entities import (
    Connection,
    ConnectionType,
    Project,
    Role,
    Rule,
    RuleGroup,
    RuleRun,
    RuleType,
    Schedule,
    ScheduleCadence,
    ScheduleTargetKind,
    User,
    WorkspaceRole,
    utc_now,
)
from dqtool.services.ai import OllamaService
from dqtool.services.connectors import ODBC_SETTINGS, ConnectorService
from dqtool.services.execution import ExecutionService
from dqtool.services.profiling import ProfilingService, detect_anomalies, source_profile_key
from dqtool.services.project import (
    ProjectContext,
    get_connection_secret,
    get_or_create_storage_secret,
    load_settings,
    open_or_create_project,
    save_connection_secret,
    save_settings,
)
from dqtool.services.rules import (
    RULE_CONFIG_EXAMPLES,
    RULE_TEMPLATES,
    ancestor_group_ids,
    normalize_rule_config,
    resolve_group_rules,
    validate_rule_config,
)
from dqtool.services.scheduling import WEEKDAY_NAMES, compute_next_run, describe_cadence
from dqtool.services.workspace import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    WorkspaceContext,
    open_or_create_workspace,
)

# Chart colors: run statuses reuse the app's positive/negative/warning tokens (CVD-validated);
# identity is always carried by labels and legends too, never by color alone.
RUN_STATUS_STYLES = {
    "passed": ("Passed", "#16a34a"),
    "failed": ("Failed", "#dc2626"),
    "error": ("Error", "#d97706"),
}
CONNECTION_TYPE_LABELS = {"csv": "CSV", "oracle": "Oracle", "sqlserver": "SQL Server", "db2": "DB2", "sybase": "Sybase"}

CHART_INK = "#37332e"
CHART_MUTED = "#837d74"
CHART_GRID = "#e2ded7"
CHART_SERIES = "#6f6960"

# Shared Quasar cell templates -------------------------------------------------

# Tree Name cell used by the Rules and Results overview tables: CSS indentation by depth,
# per-kind icon, and (for groups with children) a chevron that emits `toggle_group`
# to collapse/expand the subtree without selecting the row.
TREE_NAME_CELL_TEMPLATE = r"""
    <q-td key="name" :props="props">
        <div class="row items-center no-wrap" :style="{ paddingLeft: (props.row.depth * 22) + 'px' }">
            <q-icon
                v-if="props.row.kind === 'group' && props.row.has_children"
                :name="props.row.collapsed ? 'chevron_right' : 'expand_more'"
                size="18px"
                class="q-mr-xs cursor-pointer"
                style="color: #6f6960"
                @click.stop="() => $parent.$emit('toggle_group', props.row)"
            />
            <div v-else style="width: 22px; display: inline-block;"></div>
            <q-icon
                v-if="props.row.depth > 0"
                name="subdirectory_arrow_right"
                size="16px"
                class="q-mr-xs"
                style="color: #837d74"
            />
            <q-icon
                v-if="props.row.kind === 'group'"
                name="folder"
                size="16px"
                class="q-mr-xs"
                style="color: #6f6960"
            />
            <q-icon
                v-else
                name="checklist"
                size="16px"
                class="q-mr-xs"
                style="color: #8d8579"
            />
            <span :style="(props.row.kind === 'group' && props.row.depth === 0) ? 'font-weight:700' : ''">{{ props.row.name }}</span>
        </div>
    </q-td>
"""

# PASSED/FAILED/ERROR badge used by the Results and Schedules tables.
STATUS_BADGE_CELL_TEMPLATE = r"""
    <q-td key="last_status" :props="props">
        <q-badge
            v-if="props.row.last_status !== '-'"
            :color="props.row.last_status === 'PASSED' ? 'positive' : (props.row.last_status === 'FAILED' ? 'negative' : 'warning')"
            text-color="white"
            :label="props.row.last_status"
        />
        <span v-else>-</span>
    </q-td>
"""

# Official Colruyt Group mark (cropped from the full logo); SVG below is the fallback.
LOGO_MARK_PATH = Path(__file__).parent / "assets" / "logo_mark.png"

# Colruyt Group "G" mark, recreated as an inline SVG (brand grey #a8a29a).
COLRUYT_LOGO_SVG = (
    '<svg viewBox="0 0 200 200" width="30" height="30" xmlns="http://www.w3.org/2000/svg" aria-label="Colruyt Group">'
    '<mask id="dq-logo-mask">'
    '<rect width="200" height="200" fill="white"/>'
    '<circle cx="112" cy="78" r="30" fill="black"/>'
    '<rect x="82" y="0" width="118" height="78" fill="black"/>'
    "</mask>"
    '<circle cx="100" cy="100" r="100" fill="#a8a29a" mask="url(#dq-logo-mask)"/>'
    "</svg>"
)


class DQToolWebApp:
    def __init__(self, workspace: WorkspaceContext | None = None, user: User | None = None) -> None:
        self.connector_service = ConnectorService()
        self.execution_service = ExecutionService(self.connector_service)
        self.profiling_service = ProfilingService(self.connector_service)
        self.ollama_service = OllamaService()
        self._last_anomaly_report: dict[str, Any] | None = None
        self.workspace = workspace
        self.project: ProjectContext | None = None
        self.current_project: Project | None = None
        self.current_user: str = user.username if user else ""
        self.workspace_role = user.role if user else WorkspaceRole.MEMBER
        self.current_role = Role.USER  # role inside the currently open project
        self.signed_in = user is not None
        self._selected_username: str | None = None
        self.selected_connection_id: str | None = None
        self.selected_item_key: str | None = None
        self.selected_result_rule_id: str | None = None
        self.selected_run_id: str | None = None
        self._overview_all_rows: list[dict[str, Any]] = []
        self._overview_collapsed: set[str] = set()
        self._checked_rule_keys: set[str] = set()
        self._results_all_rows: list[dict[str, Any]] = []
        self._results_collapsed: set[str] = set()

        self.project_select: ui.select
        self.user_name_label: ui.label
        self.user_role_label: ui.label
        self.status_label: ui.label
        self.dashboard_markdown: ui.markdown
        self.result_details: ui.markdown
        self.preview_log: ui.markdown
        self.failed_rows_label: ui.label
        self.workspace_hint: ui.label
        self.last_action_label: ui.label

        self.outcomes_chart: ui.echart
        self.top_failures_chart: ui.echart
        self.results_outcome_chart: ui.echart
        self.results_trend_chart: ui.echart
        self.anomaly_nulls_chart: ui.echart
        self.anomaly_rowcount_chart: ui.echart

        self.connections_count: ui.label
        self.rules_count: ui.label
        self.runs_count: ui.label
        self.failures_count: ui.label

        self.members_title: ui.label
        self.members_table: ui.table
        self.member_project_select: ui.select
        self.member_user_select: ui.select
        self.member_role_select: ui.select

        self.connections_table: ui.table
        self.overview_table: ui.table
        self.rule_summary_table: ui.table
        self.results_table: ui.table
        self.users_table: ui.table
        self.preview_table: ui.table
        self.failed_rows_table: ui.table
        self.schedules_table: ui.table

        self.connection_select: ui.select
        self.item_select: ui.select
        self.run_checked_button: ui.button
        self.schedule_select: ui.select
        self.overview_search: ui.input
        self.results_search: ui.input
        self.result_rule_select: ui.select
        self.result_select: ui.select
        self.preview_connection_select: ui.select
        self.preview_target_select: ui.select
        self.anomaly_connection_select: ui.select
        self.anomaly_target_select: ui.select
        self.anomaly_summary: ui.markdown
        self.anomaly_table: ui.table
        self.profile_table: ui.table
        self.ai_explanation: ui.markdown

        self.export_path_input: ui.input
        self.import_path_input: ui.input

    def build(self) -> None:
        ui.page_title("DQTool")
        ui.colors(
            primary="#6f6960",
            secondary="#e30613",
            accent="#8d8579",
            positive="#16a34a",
            negative="#dc2626",
            warning="#d97706",
        )
        ui.add_head_html(
            """
            <style>
              :root {
                --dq-ink: #37332e;
                --dq-muted: #837d74;
                --dq-teal: #6f6960;
                --dq-teal-dark: #4e4943;
                --dq-mint: #edeae4;
                --dq-orange: #e30613;
                --dq-sky: #8d8579;
                --dq-cream: #f6f4f0;
                --dq-line: #e2ded7;
              }
              body { background: var(--dq-cream); color: var(--dq-ink); }
              body, .q-field, .q-btn, .q-table { font-family: "Aptos", "Trebuchet MS", sans-serif; }
              .nicegui-content { padding: 0 !important; }
              .dq-shell {
                min-height: 100vh;
                background:
                  radial-gradient(circle at 91% 4%, rgba(141, 133, 121, .14), transparent 24rem),
                  linear-gradient(120deg, rgba(237, 234, 228, .6), transparent 42%),
                  var(--dq-cream);
              }
              .dq-app-frame {
                min-height: 100vh;
                align-items: stretch;
              }
              .dq-sidebar {
                width: 264px;
                flex: 0 0 264px;
                min-height: 100vh;
                color: white;
                background:
                  radial-gradient(circle at 15% 85%, rgba(227, 6, 19, .10), transparent 18rem),
                  linear-gradient(165deg, #3e3a34 0%, #4a453e 54%, #57524a 100%);
                box-shadow: 12px 0 34px rgba(40, 37, 32, .14);
                position: relative;
                overflow: hidden;
              }
              .dq-sidebar::before {
                content: "";
                position: absolute;
                width: 180px;
                height: 180px;
                border: 1px solid rgba(255,255,255,.11);
                border-radius: 50%;
                top: -82px;
                right: -90px;
              }
              .dq-main {
                min-width: 0;
                max-width: 1600px;
                margin: 0 auto;
                padding: 28px 34px 44px;
              }
              .dq-brand-mark {
                width: 44px;
                height: 44px;
                border-radius: 14px;
                display: grid;
                place-items: center;
                background: rgba(255, 255, 255, .12);
                color: white;
                box-shadow: 0 10px 24px rgba(0, 0, 0, .18);
              }
              .dq-eyebrow {
                color: var(--dq-teal);
                font-size: 11px;
                font-weight: 800;
                letter-spacing: .18em;
                text-transform: uppercase;
              }
              .dq-sidebar .q-tabs { color: rgba(255,255,255,.72); }
              .dq-sidebar .q-tab {
                min-height: 48px;
                border-radius: 13px;
                margin: 3px 0;
                padding: 0 14px;
                justify-content: flex-start;
                transition: background .18s ease, color .18s ease, transform .18s ease;
              }
              .dq-sidebar .q-tab:hover { background: rgba(255,255,255,.08); color: white; transform: translateX(2px); }
              .dq-sidebar .q-tab--active {
                color: #4e4943 !important;
                background: #f4f2ed !important;
                box-shadow: 0 8px 20px rgba(30, 28, 24, .2);
              }
              .dq-sidebar .q-tab__indicator { display: none; }
              .dq-sidebar .q-tab__icon { font-size: 20px; margin-right: 12px; }
              .dq-status-box {
                background: rgba(255,255,255,.09);
                border: 1px solid rgba(255,255,255,.13);
                border-radius: 16px;
                backdrop-filter: blur(8px);
              }
              .dq-soft-card {
                border-radius: 20px !important;
                background: rgba(255,255,255,.92) !important;
                box-shadow: 0 10px 32px rgba(55, 51, 46, .07) !important;
                border: 1px solid rgba(214, 209, 200, .88) !important;
              }
              .dq-stat-card {
                border-radius: 18px !important;
                min-height: 124px;
                overflow: hidden;
                position: relative;
                border: 1px solid rgba(214, 209, 200, .86) !important;
                box-shadow: 0 8px 24px rgba(55, 51, 46, .06) !important;
              }
              .dq-stat-card::after {
                content: "";
                width: 76px;
                height: 76px;
                border-radius: 50%;
                background: currentColor;
                opacity: .07;
                position: absolute;
                right: -20px;
                bottom: -28px;
              }
              .dq-hero {
                min-height: 210px;
                border-radius: 24px !important;
                color: white;
                overflow: hidden;
                position: relative;
                background:
                  radial-gradient(circle at 88% 18%, rgba(255,255,255,.18), transparent 9rem),
                  linear-gradient(125deg, #4e4943 0%, #6f6960 58%, #8d8579 100%) !important;
                box-shadow: 0 18px 42px rgba(78, 73, 67, .25) !important;
              }
              .dq-hero::after {
                content: "";
                position: absolute;
                width: 220px;
                height: 220px;
                border-radius: 48% 52% 68% 32%;
                border: 1px solid rgba(255,255,255,.18);
                right: -55px;
                bottom: -105px;
                transform: rotate(24deg);
              }
              .dq-project-bar {
                border-radius: 18px !important;
                border: 1px solid rgba(214, 209, 200, .9) !important;
                background: rgba(255,255,255,.84) !important;
                box-shadow: 0 8px 26px rgba(55, 51, 46, .06) !important;
              }
              .dq-panel-title { font-family: "Aptos Display", "Trebuchet MS", sans-serif; color: var(--dq-ink); }
              .dq-panel-copy { color: var(--dq-muted); line-height: 1.55; }
              .dq-dot { width: 8px; height: 8px; border-radius: 50%; background: #4ade80; box-shadow: 0 0 0 4px rgba(74,222,128,.15); }
              .dq-main .q-tab-panel { padding: 0; background: transparent; }
              .dq-main .q-tab-panels { background: transparent; }
              .dq-main .q-table__container { border-color: var(--dq-line) !important; border-radius: 14px; overflow: hidden; }
              .dq-main thead tr { background: #f0eee9; color: #5c564d; }
              .dq-main th { font-size: 11px !important; font-weight: 800 !important; letter-spacing: .08em; text-transform: uppercase; }
              .dq-main tbody tr:hover { background: #f4f2ed; }
              .dq-main .dq-selectable-table tbody tr { cursor: pointer; transition: background-color .16s ease, box-shadow .16s ease; }
              .dq-main .dq-selectable-table tbody tr.selected { background: #e9e6df !important; box-shadow: inset 4px 0 0 var(--dq-teal); }
              .dq-main .q-btn { border-radius: 11px; font-weight: 700; letter-spacing: 0; }
              .dq-main .q-field--outlined .q-field__control { border-radius: 11px; }
              .dq-meta-card { background: linear-gradient(145deg, #faf7f1, #fff) !important; border-color: #e5ded2 !important; }
              .dq-section-card { border-top: 4px solid var(--dq-teal) !important; }
              .dq-table-wrap { overflow-x: auto; border-radius: 14px; }
              .q-dialog .q-card {
                border-radius: 20px !important;
                border: 1px solid var(--dq-line);
                box-shadow: 0 24px 70px rgba(40, 37, 32, .22) !important;
                padding: 24px;
              }
              .q-dialog .q-expansion-item {
                border: 1px solid var(--dq-line);
                border-radius: 12px;
                overflow: hidden;
              }
              .dq-connection-choice {
                display: grid !important;
                grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
                gap: 10px;
                width: 100%;
              }
              .dq-connection-choice .q-radio {
                margin: 0;
                padding: 11px 13px;
                border: 1px solid var(--dq-line);
                border-radius: 12px;
                background: #f8f7f4;
                transition: border-color .18s ease, background .18s ease, box-shadow .18s ease;
              }
              .dq-connection-choice .q-radio:has(.q-radio__inner--truthy) {
                border-color: var(--dq-teal);
                background: #efece6;
                box-shadow: 0 0 0 3px rgba(111, 105, 96, .12);
              }
              .dq-reference-panel {
                width: 100%;
                padding: 18px;
                border: 1px solid #d8d3ca;
                border-radius: 16px;
                background: linear-gradient(145deg, #f4f2ed, #ffffff);
              }
              .dq-scroll-dialog { max-height: 92vh !important; overflow-y: auto !important; overscroll-behavior: contain; }
              .dq-rule-dialog { max-height: 92vh !important; overflow-y: auto !important; overscroll-behavior: contain; }
              @media (max-width: 900px) {
                .dq-app-frame { flex-direction: column; }
                .dq-sidebar { width: 100%; flex-basis: auto; min-height: auto; }
                .dq-sidebar-nav { flex-direction: row !important; overflow-x: auto; }
                .dq-sidebar .q-tabs__content { flex-direction: row !important; }
                .dq-sidebar .q-tab { min-width: 132px; }
                .dq-sidebar-footer { display: none !important; }
                .dq-main { padding: 20px 16px 36px; }
              }
              @media (max-width: 600px) {
                .dq-page-heading { font-size: 28px !important; }
                .dq-hero { min-height: 250px; }
              }
            </style>
            """
        )

        with ui.column().classes("dq-shell w-full gap-0"):
            with ui.row().classes("dq-app-frame w-full gap-0 no-wrap"):
                with ui.column().classes("dq-sidebar gap-6 p-5"):
                    with ui.row().classes("items-center gap-3 px-1"):
                        with ui.element("div").classes("dq-brand-mark"):
                            if LOGO_MARK_PATH.is_file():
                                ui.image(str(LOGO_MARK_PATH)).classes("w-[30px] h-[30px]")
                            else:
                                ui.html(COLRUYT_LOGO_SVG)
                        with ui.column().classes("gap-0"):
                            ui.label("DQTool").classes("text-2xl font-bold tracking-tight text-white")
                            ui.label("COLRUYT GROUP").classes("text-[9px] font-bold tracking-[0.22em] text-stone-300")

                    ui.label("MENU").classes("px-3 text-[10px] font-bold tracking-[0.22em] text-stone-300/70")
                    with ui.tabs().props("vertical inline-label").classes("dq-sidebar-nav w-full") as tabs:
                        dashboard_tab = ui.tab("Dashboard", icon="dashboard").classes("justify-start")
                        connections_tab = ui.tab("Connections", icon="cable").classes("justify-start")
                        rules_tab = ui.tab("Rules", icon="rule").classes("justify-start")
                        schedules_tab = ui.tab("Schedules", icon="schedule").classes("justify-start")
                        results_tab = ui.tab("Results", icon="task_alt").classes("justify-start")
                        anomalies_tab = ui.tab("Anomalies", icon="troubleshoot").classes("justify-start")
                        users_tab = ui.tab("Users", icon="group").classes("justify-start")
                        preview_tab = ui.tab("Preview", icon="preview").classes("justify-start")

                    ui.space()
                    with ui.column().classes("dq-sidebar-footer dq-status-box w-full gap-2 p-4"):
                        with ui.row().classes("items-center gap-2"):
                            ui.element("span").classes("dq-dot")
                            ui.label("Workspace status").classes("text-xs font-bold uppercase tracking-wider text-stone-200")
                        self.status_label = ui.label("No project open").classes("text-sm font-semibold text-white")
                        self.workspace_hint = ui.label(
                            "Use a writable folder under Documents or Downloads."
                        ).classes("text-xs leading-5 text-stone-200/70")
                        self.last_action_label = ui.label("No actions yet.").classes("text-xs leading-5 text-stone-200/70")

                with ui.column().classes("dq-main grow gap-5"):
                    with ui.row().classes("w-full items-center justify-between gap-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("DATA QUALITY CONTROL").classes("dq-eyebrow")
                            ui.label("Your quality workspace").classes("dq-page-heading text-4xl font-bold tracking-tight text-[#37332e]")
                        with ui.row().classes("items-center gap-3"):
                            with ui.column().classes("hidden sm:flex gap-0 items-end"):
                                self.user_name_label = ui.label(self.current_user or "Not signed in").classes(
                                    "text-sm font-bold text-[#37332e]"
                                )
                                self.user_role_label = ui.label(self.workspace_role.value).classes("text-xs text-[#837d74]")
                            with ui.avatar(color="primary", text_color="white"):
                                ui.icon("person")
                            ui.button(icon="logout", on_click=self.sign_out).props("flat round color=primary").tooltip(
                                "Sign out"
                            )

                    with ui.card().classes("dq-project-bar w-full p-4"):
                        with ui.row().classes("w-full items-center gap-3"):
                            with ui.element("div").classes("hidden md:grid w-10 h-10 rounded-xl bg-stone-100 place-items-center"):
                                ui.icon("workspaces", color="primary").classes("text-xl")
                            self.project_select = ui.select(options={}, label="Project").props("outlined dense").classes(
                                "grow min-w-[220px] max-w-[380px]"
                            )
                            ui.button("Open project", icon="launch", on_click=self.open_selected_project).props(
                                "color=primary unelevated no-caps"
                            )
                            ui.button("New project", icon="create_new_folder", on_click=self.show_new_project_dialog).props(
                                "outline no-caps color=primary"
                            )
                            ui.button(icon="refresh", on_click=self.refresh_all).props("flat round color=primary").tooltip(
                                "Refresh project data"
                            )

                    with ui.tab_panels(tabs, value=dashboard_tab).classes("w-full min-w-0"):
                        with ui.tab_panel(dashboard_tab):
                            with ui.column().classes("w-full gap-5"):
                                with ui.card().classes("dq-hero w-full p-7 md:p-8"):
                                    with ui.column().classes("relative z-10 max-w-2xl gap-3"):
                                        ui.label("MAKE EVERY ROW COUNT").classes("text-xs font-bold tracking-[0.2em] text-stone-300")
                                        ui.label("Trust your data before it travels.").classes("text-3xl md:text-4xl font-bold leading-tight")
                                        ui.label(
                                            "Connect sources, turn expectations into reusable rules, and investigate failures from one shared workspace."
                                        ).classes("max-w-xl text-sm md:text-base leading-6 text-stone-200/80")
                                        with ui.row().classes("gap-3 mt-2"):
                                            ui.button("Create a rule", icon="add", on_click=lambda: self.show_rule_dialog()).props(
                                                "unelevated no-caps color=secondary"
                                            )
                                            ui.button("Refresh overview", icon="refresh", on_click=self.refresh_all).props(
                                                "outline no-caps color=white"
                                            )

                                with ui.row().classes("w-full gap-4 items-stretch"):
                                    self._stat_block("Connections", "0", "cable", "#6f6960", "#efece6")
                                    self._stat_block("Rules", "0", "rule", "#8d8579", "#f0eee9")
                                    self._stat_block("Runs", "0", "play_circle", "#b45309", "#f9f0e3")
                                    self._stat_block("Failures", "0", "error_outline", "#dc2626", "#fff0f0")

                                with ui.row().classes("w-full items-stretch gap-5"):
                                    with ui.card().classes("dq-soft-card w-full lg:w-[calc(38%-10px)] p-6"):
                                        ui.label("OUTCOMES").classes("dq-eyebrow")
                                        ui.label("Recent run results").classes("dq-panel-title text-xl font-bold")
                                        self.outcomes_chart = ui.echart(self._empty_chart_options("No runs yet")).classes(
                                            "w-full h-[280px]"
                                        )
                                    with ui.card().classes("dq-soft-card w-full lg:w-[calc(62%-10px)] p-6"):
                                        ui.label("HOTSPOTS").classes("dq-eyebrow")
                                        ui.label("Failed rows by rule (latest run)").classes("dq-panel-title text-xl font-bold")
                                        self.top_failures_chart = ui.echart(self._empty_chart_options("No runs yet")).classes(
                                            "w-full h-[280px]"
                                        )

                                with ui.row().classes("w-full items-stretch gap-5"):
                                    with ui.card().classes("dq-soft-card w-full lg:w-[calc(62%-10px)] p-6"):
                                        ui.label("ACTIVITY").classes("dq-eyebrow")
                                        ui.label("Project overview").classes("dq-panel-title text-2xl font-bold")
                                        self.dashboard_markdown = ui.markdown("Open a project to begin.").classes("w-full dq-panel-copy mt-2")

                                    with ui.card().classes("dq-soft-card dq-meta-card w-full lg:w-[calc(38%-10px)] p-6"):
                                        with ui.row().classes("items-center gap-3"):
                                            with ui.element("div").classes("grid w-11 h-11 rounded-xl bg-stone-200 place-items-center"):
                                                ui.icon("swap_vert", color="secondary").classes("text-2xl")
                                            with ui.column().classes("gap-0"):
                                                ui.label("Metadata transfer").classes("dq-panel-title text-xl font-bold")
                                                ui.label("Move project definitions as JSON").classes("text-xs text-[#837d74]")
                                        with ui.column().classes("w-full gap-3 mt-3"):
                                            self.export_path_input = ui.input(
                                                "Export path",
                                                placeholder=r"C:\Users\<you>\Downloads\dqtool-metadata.json",
                                            ).props("outlined dense").classes("w-full")
                                            ui.button("Export metadata", icon="file_upload", on_click=self.export_metadata).props(
                                                "outline no-caps color=primary"
                                            ).classes("self-start")
                                            ui.separator().classes("my-1")
                                            self.import_path_input = ui.input(
                                                "Import path",
                                                placeholder=r"C:\Users\<you>\Downloads\dqtool-metadata.json",
                                            ).props("outlined dense").classes("w-full")
                                            ui.button("Import metadata", icon="file_download", on_click=self.import_metadata).props(
                                                "unelevated no-caps color=primary"
                                            ).classes("self-start")

                        with ui.tab_panel(connections_tab):
                            self._build_connections_tab()

                        with ui.tab_panel(rules_tab):
                            self._build_rules_tab()

                        with ui.tab_panel(schedules_tab):
                            self._build_schedules_tab()

                        with ui.tab_panel(results_tab):
                            self._build_results_tab()

                        with ui.tab_panel(anomalies_tab):
                            self._build_anomalies_tab()

                        with ui.tab_panel(users_tab):
                            self._build_users_tab()

                        with ui.tab_panel(preview_tab):
                            self._build_preview_tab()

        if self.workspace and self.signed_in:
            projects = self.workspace.storage.projects_for_user(self.current_user)
            self.user_role_label.text = f"{self.workspace_role.value} | {len(projects)} project(s)"
            self.status_label.text = (
                f"Workspace: {self.workspace.root_dir.name} | User: {self.current_user} ({self.workspace_role.value})"
            )
            self.workspace_hint.text = "Pick a project you have access to and open it."
            self.refresh_all()
            self._populate_project_options()
            self._open_recent_project()
            if (
                self.workspace_role == WorkspaceRole.WORKSPACE_ADMIN
                and self.workspace.storage.verify_login(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD) is not None
            ):
                ui.notify(
                    "The default admin password is still in place. Change it in the Users tab.",
                    type="warning",
                )

    def _build_connections_tab(self) -> None:
        with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
            with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("SOURCES").classes("dq-eyebrow")
                    ui.label("Connections").classes("dq-panel-title text-2xl font-bold")
                    ui.label("Store database and CSV connection definitions with shared visibility rules.").classes(
                        "dq-panel-copy text-sm"
                    )
                with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                    self.connection_select = ui.select(options={}, label="Selected connection").props("outlined dense").classes(
                        "grow min-w-[230px] max-w-[380px]"
                    )
                    self.connection_select.on_value_change(
                        lambda event: self._highlight_table_row(self.connections_table, event.value)
                    )
                    ui.button("Add", icon="add", on_click=lambda: self.show_connection_dialog()).props(
                        "color=primary unelevated no-caps"
                    )
                    ui.button("Edit", icon="edit", on_click=self.edit_selected_connection).props("outline no-caps")
                    ui.button("Test", icon="network_check", on_click=self.test_selected_connection).props(
                        "color=secondary unelevated no-caps"
                    )
                    ui.button("Delete", icon="delete", on_click=self.delete_selected_connection).props(
                        "outline no-caps color=negative"
                    )
            self.connections_table = self._build_table(["ID", "Name", "Type", "Owner", "Visibility"])
            self.connections_table.classes(add="dq-selectable-table")
            self.connections_table.on("rowClick", self._select_connection_row)

    def _build_rules_tab(self) -> None:
        with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
            with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("CHECKS & BATCHES").classes("dq-eyebrow")
                    ui.label("Rules & rule groups").classes("dq-panel-title text-2xl font-bold")
                    ui.label(
                        "One overview of every rule and rule group. Groups show their nested subgroups "
                        "and member rules indented underneath, in a single tree."
                    ).classes("dq-panel-copy text-sm")
                with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                    self.item_select = ui.select(options={}, label="Selected item").props("outlined dense").classes(
                        "grow min-w-[230px] max-w-[380px]"
                    )
                    self.item_select.on_value_change(self._on_item_select_change)
                    ui.button("Add rule", icon="add", on_click=lambda: self.show_rule_dialog()).props(
                        "color=primary unelevated no-caps"
                    )
                    ui.button("Add group", icon="create_new_folder", on_click=lambda: self.show_group_dialog()).props(
                        "outline no-caps"
                    )
                    ui.button("Move to group", icon="drive_file_move", on_click=self.move_selected_rule_to_group).props(
                        "outline no-caps"
                    )
                    ui.button("Edit", icon="edit", on_click=self.edit_selected_item).props("outline no-caps")
                    ui.button("Run", icon="play_arrow", on_click=self.run_selected_item).props(
                        "color=secondary unelevated no-caps"
                    )
                    ui.button("Delete", icon="delete", on_click=self.delete_selected_item).props(
                        "outline no-caps color=negative"
                    )
            with ui.row().classes("w-full items-center justify-end gap-2 mt-2"):
                ui.label("Check rules below to run just those.").classes("dq-panel-copy text-xs text-[#837d74] grow")
                self.run_checked_button = ui.button(
                    "Run selected (0)", icon="playlist_play", on_click=self.run_checked_rules
                ).props("outline no-caps")
                self.run_checked_button.visible = False
            with ui.row().classes("w-full items-center gap-2 mt-3"):
                self.overview_search = ui.input(placeholder="Search rules and groups...").props(
                    "outlined dense clearable prepend-icon=search"
                ).classes("w-full max-w-md")
                self.overview_search.on_value_change(lambda _event: self._refresh_overview_view())
            self.overview_table = ui.table(
                columns=self._columns(["Select", "ID", "Name", "Kind", "Details", "Owner", "Visibility", "Used In"]),
                rows=[],
                row_key="key",
                pagination=10,
            ).props("flat bordered wrap-cells").classes("dq-table-wrap w-full mt-4")
            self.overview_table.classes(add="dq-selectable-table")
            self.overview_table.on("rowClick", self._select_overview_row)
            self.overview_table.on("toggle_group", self._on_toggle_group)
            self.overview_table.on("toggle_rule_check", self._on_toggle_rule_check)
            # Checkbox lets a specific rule be queued for a batch "Run selected" without changing
            # the single-item selection used by the Edit/Run/Delete buttons above. Groups have no
            # checkbox: running a group already runs every rule nested under it.
            self.overview_table.add_slot(
                "body-cell-select",
                r"""
                <q-td key="select" :props="props" @click.stop>
                    <q-checkbox
                        v-if="props.row.kind === 'rule'"
                        :model-value="props.row.checked"
                        dense
                        @update:model-value="() => $parent.$emit('toggle_rule_check', props.row)"
                    />
                </q-td>
                """,
            )
            self.overview_table.add_slot("body-cell-name", TREE_NAME_CELL_TEMPLATE)
            # Colors visibility so private/shared/shared_specific reads at a glance instead of as plain text.
            self.overview_table.add_slot(
                "body-cell-visibility",
                r"""
                <q-td key="visibility" :props="props">
                    <q-badge
                        :color="props.row.visibility === 'private' ? 'grey-6' : (props.row.visibility === 'shared' ? 'positive' : 'warning')"
                        text-color="white"
                        :label="props.row.visibility"
                    />
                </q-td>
                """,
            )

    def _build_schedules_tab(self) -> None:
        with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
            with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("AUTOMATION").classes("dq-eyebrow")
                    ui.label("Schedules").classes("dq-panel-title text-2xl font-bold")
                    ui.label(
                        "Run a rule or rule group automatically. Times are UTC; the scheduler checks every "
                        "minute, and only fires while the DQTool app process itself is running."
                    ).classes("dq-panel-copy text-sm")
                with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                    self.schedule_select = ui.select(options={}, label="Selected schedule").props(
                        "outlined dense"
                    ).classes("grow min-w-[230px] max-w-[380px]")
                    self.schedule_select.on_value_change(self._on_schedule_select_change)
                    ui.button("Add schedule", icon="add", on_click=lambda: self.show_schedule_dialog()).props(
                        "color=primary unelevated no-caps"
                    )
                    ui.button("Edit", icon="edit", on_click=self.edit_selected_schedule).props("outline no-caps")
                    ui.button(
                        "Enable/Disable", icon="power_settings_new", on_click=self.toggle_selected_schedule
                    ).props("outline no-caps")
                    ui.button("Run now", icon="play_arrow", on_click=self.run_selected_schedule_now).props(
                        "color=secondary unelevated no-caps"
                    )
                    ui.button("Delete", icon="delete", on_click=self.delete_selected_schedule).props(
                        "outline no-caps color=negative"
                    )
            self.schedules_table = ui.table(
                columns=self._columns(
                    ["ID", "Name", "Target", "Cadence", "Enabled", "Next Run", "Last Run", "Last Status"]
                ),
                rows=[],
                row_key="key",
                pagination=10,
            ).props("flat bordered wrap-cells").classes("dq-table-wrap w-full mt-4")
            self.schedules_table.classes(add="dq-selectable-table")
            self.schedules_table.on("rowClick", self._select_schedule_row)
            self.schedules_table.add_slot("body-cell-last_status", STATUS_BADGE_CELL_TEMPLATE)
            self.schedules_table.add_slot(
                "body-cell-enabled",
                r"""
                <q-td key="enabled" :props="props">
                    <q-badge
                        :color="props.row.enabled === 'Yes' ? 'positive' : 'grey-6'"
                        text-color="white"
                        :label="props.row.enabled"
                    />
                </q-td>
                """,
            )

    def _build_results_tab(self) -> None:
        with ui.column().classes("w-full gap-4"):
            with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
                with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("HISTORY").classes("dq-eyebrow")
                        ui.label("Execution results").classes("dq-panel-title text-2xl font-bold")
                        ui.label(
                            "Rules grouped the same way as the Rules tab. Select a rule to inspect its "
                            "executions below; group rows summarize the rules nested under them."
                        ).classes("dq-panel-copy text-sm")
                    with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                        self.result_rule_select = ui.select(options={}, label="Selected rule").props("outlined dense").classes(
                            "grow min-w-[230px] max-w-[380px]"
                        )
                        self.result_rule_select.on_value_change(self._on_result_item_select_change)
                with ui.row().classes("w-full items-center gap-2 mt-2"):
                    self.results_search = ui.input(placeholder="Search rules and groups...").props(
                        "outlined dense clearable prepend-icon=search"
                    ).classes("w-full max-w-md")
                    self.results_search.on_value_change(lambda _event: self._refresh_results_view())
                self.rule_summary_table = ui.table(
                    columns=self._columns(["ID", "Name", "Kind", "Details", "Runs", "Last Status", "Last Run", "Last Failed"]),
                    rows=[],
                    row_key="key",
                    pagination=10,
                ).props("flat bordered wrap-cells").classes("dq-table-wrap w-full mt-4")
                self.rule_summary_table.classes(add="dq-selectable-table")
                self.rule_summary_table.on("rowClick", self._select_result_rule_row)
                self.rule_summary_table.on("toggle_group", self._on_toggle_results_group)
                # Same tree treatment as the Rules tab: indentation + per-kind icon + collapse chevron.
                self.rule_summary_table.add_slot("body-cell-name", TREE_NAME_CELL_TEMPLATE)
                # Colors the aggregated/last status so passed/failed/error reads at a glance.
                self.rule_summary_table.add_slot("body-cell-last_status", STATUS_BADGE_CELL_TEMPLATE)
            with ui.card().classes("dq-soft-card w-full p-6"):
                with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("DETAIL").classes("dq-eyebrow")
                        ui.label("Executions of the selected rule").classes("dq-panel-title text-xl font-bold")
                        ui.label("Every run of the rule, newest first. Pick a run for its summary and failed rows.").classes(
                            "dq-panel-copy text-sm"
                        )
                    with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                        self.result_select = ui.select(options={}, label="Selected run").props("outlined dense").classes(
                            "grow min-w-[230px] max-w-[380px]"
                        )
                        self.result_select.on_value_change(
                            lambda event: self._highlight_table_row(self.results_table, event.value)
                        )
                        ui.button("Details", icon="subject", on_click=self.view_selected_result).props("outline no-caps")
                        ui.button("Failed rows", icon="table_view", on_click=self.preview_selected_failed_rows).props(
                            "color=primary unelevated no-caps"
                        )
                        ui.button("Delete", icon="delete", on_click=self.delete_selected_result).props(
                            "outline no-caps color=negative"
                        )
                self.results_table = self._build_table(
                    ["Run", "Status", "Checked", "Failed", "Started", "Failed Rows File"]
                )
                self.results_table.classes(add="dq-selectable-table")
                self.results_table.on("rowClick", self._select_result_row)
            with ui.row().classes("w-full items-stretch gap-4"):
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("TREND").classes("dq-eyebrow")
                    ui.label("Run outcomes per day").classes("dq-panel-title text-xl font-bold")
                    self.results_outcome_chart = ui.echart(self._empty_chart_options("No runs yet")).classes(
                        "w-full h-[260px]"
                    )
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("RULE HISTORY").classes("dq-eyebrow")
                    ui.label("Failed rows over time (selected rule)").classes("dq-panel-title text-xl font-bold")
                    self.results_trend_chart = ui.echart(self._empty_chart_options("Select a rule")).classes(
                        "w-full h-[260px]"
                    )
            with ui.row().classes("w-full items-stretch gap-4"):
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(40%-8px)] p-6"):
                    ui.label("Run details").classes("dq-panel-title text-xl font-bold")
                    self.result_details = ui.markdown("Select a run to view its details.").classes("w-full")
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(60%-8px)] p-6"):
                    ui.label("Failed row preview").classes("dq-panel-title text-xl font-bold")
                    self.failed_rows_label = ui.label("Select a failed result to preview rows.").classes("text-sm text-slate-700")
                    self.failed_rows_table = self._build_table([], pagination=8)

    def _build_anomalies_tab(self) -> None:
        with ui.column().classes("w-full gap-4"):
            with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
                with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("MONITOR").classes("dq-eyebrow")
                        ui.label("Anomaly check").classes("dq-panel-title text-2xl font-bold")
                        ui.label(
                            "Profile a connection's file or table and compare it with the previous snapshot to spot drift: "
                            "row count jumps, null spikes, vanished columns, and shifted averages."
                        ).classes("dq-panel-copy text-sm")
                    with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                        self.anomaly_connection_select = ui.select(
                            options={}, label="Connection", on_change=self._load_anomaly_targets
                        ).props("outlined dense").classes("grow min-w-[190px] max-w-[280px]")
                        self.anomaly_target_select = ui.select(
                            options=[], label="File / table", with_input=True
                        ).props("outlined dense").classes("grow min-w-[190px] max-w-[320px]")
                        ui.button("Run anomaly check", icon="troubleshoot", on_click=self.run_anomaly_check).props(
                            "color=primary unelevated no-caps"
                        )
                        ui.button("Explain with AI", icon="psychology", on_click=self.explain_selected_anomalies).props(
                            "outline no-caps"
                        ).tooltip("Uses a local Ollama model; no data leaves this machine")
                self.anomaly_summary = ui.markdown(
                    "Select a connection and a file or table, then run a check to build the first baseline."
                ).classes("w-full mt-2")
            with ui.row().classes("w-full items-stretch gap-4"):
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("COMPLETENESS").classes("dq-eyebrow")
                    ui.label("Null rate by column").classes("dq-panel-title text-xl font-bold")
                    self.anomaly_nulls_chart = ui.echart(
                        self._empty_chart_options("Run an anomaly check to see charts")
                    ).classes("w-full h-[260px]")
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("VOLUME").classes("dq-eyebrow")
                    ui.label("Row count across snapshots").classes("dq-panel-title text-xl font-bold")
                    self.anomaly_rowcount_chart = ui.echart(
                        self._empty_chart_options("Run an anomaly check to see charts")
                    ).classes("w-full h-[260px]")
            with ui.row().classes("w-full items-stretch gap-4"):
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("Drift findings").classes("dq-panel-title text-xl font-bold")
                    self.anomaly_table = self._build_table(["Severity", "Column", "Finding"], pagination=8)
                with ui.card().classes("dq-soft-card w-full lg:w-[calc(50%-8px)] p-6"):
                    ui.label("Column profile").classes("dq-panel-title text-xl font-bold")
                    self.profile_table = ui.table(
                        columns=[
                            {"name": "field", "label": "Field", "field": "field", "align": "left"},
                            {"name": "type", "label": "Type", "field": "type", "align": "left"},
                            {"name": "null_rate", "label": "Null %", "field": "null_rate", "align": "right"},
                            {"name": "distinct", "label": "Distinct", "field": "distinct", "align": "right"},
                            {"name": "mean", "label": "Mean", "field": "mean", "align": "right"},
                        ],
                        rows=[],
                        row_key="field",
                        pagination=8,
                    ).props("flat bordered wrap-cells").classes("dq-table-wrap w-full mt-4")
            with ui.card().classes("dq-soft-card w-full p-6"):
                ui.label("AI explanation").classes("dq-panel-title text-xl font-bold")
                self.ai_explanation = ui.markdown(
                    "Run a check, then use *Explain with AI* to have a local Ollama model describe the findings."
                ).classes("w-full")

    async def run_anomaly_check(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        connection = self._anomaly_connection()
        if connection is None:
            ui.notify("Select a connection first.", type="warning")
            return
        target = str(self.anomaly_target_select.value or "").strip()
        if not target:
            ui.notify("Select a file or table first.", type="warning")
            return
        source_kind = "csv_file" if connection.connection_type == ConnectionType.CSV else "oracle_table"
        source_config = {
            "source_connection_id": int(connection.id or 0),
            "source_kind": source_kind,
            "source_name": target,
            "source_sql": "",
        }
        connections = {item.id: item for item in self._visible_connections() if item.id is not None}
        self.anomaly_summary.content = f"_Profiling **{target}**..._"
        self.anomaly_summary.update()
        try:
            profile = await nicegui_run.io_bound(self.profiling_service.profile_rule_source, source_config, connections)
        except Exception as exc:
            self.anomaly_summary.content = f"Could not profile **{target}**: {exc}"
            self.anomaly_summary.update()
            ui.notify(str(exc), type="negative")
            return
        key = source_profile_key(source_config)
        previous = self.project.storage.latest_source_profile(key)
        self.project.storage.save_source_profile(key, profile)
        anomalies = detect_anomalies(previous, profile)
        source_label = target
        self._last_anomaly_report = {"source_label": source_label, "profile": profile, "anomalies": anomalies}

        self.profile_table.rows = [
            {
                "field": name,
                "type": stats.get("type", ""),
                "null_rate": f"{float(stats.get('null_rate') or 0):.1%}",
                "distinct": stats.get("distinct_count", ""),
                "mean": "" if stats.get("mean") is None else f"{stats['mean']:.4g}",
            }
            for name, stats in profile.get("columns", {}).items()
        ]
        self.profile_table.update()
        self.anomaly_table.rows = [
            {
                "id": index,
                "severity": finding["severity"].upper(),
                "column": finding["column"] or "-",
                "finding": finding["message"],
            }
            for index, finding in enumerate(anomalies)
        ]
        self.anomaly_table.update()
        self._update_anomaly_charts(profile, key)

        if previous is None:
            message = (
                f"Baseline saved for **{source_label}** ({profile['row_count']:,} rows, "
                f"{len(profile.get('columns', {}))} columns). Run the check again after the data changes to detect drift."
            )
            if anomalies:
                message += f" Found **{len(anomalies)}** content finding(s) in this snapshot already."
            self.anomaly_summary.content = message
        elif not anomalies:
            self.anomaly_summary.content = (
                f"No anomalies found for **{source_label}** compared with the snapshot from "
                f"{self._format_timestamp(previous.get('profiled_at'))}."
            )
        else:
            high = sum(1 for finding in anomalies if finding["severity"] == "high")
            self.anomaly_summary.content = (
                f"Found **{len(anomalies)}** finding(s) for **{source_label}** "
                f"({high} high severity), including drift versus the snapshot from "
                f"{self._format_timestamp(previous.get('profiled_at'))}."
            )
        self.anomaly_summary.update()
        self._set_last_action(f"Ran anomaly check for {target}")

    def _connection_from_select(self, select: ui.select) -> Connection | None:
        selected = select.value
        if not selected:
            return None
        return next((item for item in self._visible_connections() if str(item.id) == str(selected)), None)

    def _anomaly_connection(self) -> Connection | None:
        return self._connection_from_select(self.anomaly_connection_select)

    async def _load_anomaly_targets(self, _event: Any = None) -> None:
        await self._load_connection_targets(self.anomaly_connection_select, self.anomaly_target_select)

    async def _load_preview_targets(self, _event: Any = None) -> None:
        await self._load_connection_targets(self.preview_connection_select, self.preview_target_select)

    async def _load_connection_targets(self, connection_select: ui.select, target_select: ui.select) -> None:
        connection = self._connection_from_select(connection_select)
        if connection is None:
            target_select.options = []
            target_select.value = None
            target_select.update()
            return
        try:
            targets = await nicegui_run.io_bound(self.connector_service.list_connection_targets, connection)
        except Exception as exc:
            targets = []
            ui.notify(f"Could not load files or tables: {exc}", type="negative")
        current = str(target_select.value or "")
        target_select.options = targets
        if current in targets:
            target_select.value = current
        elif len(targets) == 1:
            target_select.value = targets[0]
        else:
            target_select.value = None
        target_select.update()

    async def explain_selected_anomalies(self) -> None:
        report = self._last_anomaly_report
        if not report:
            ui.notify("Run an anomaly check first.", type="warning")
            return
        available = await nicegui_run.io_bound(self.ollama_service.is_available)
        if not available:
            ui.notify(
                f"Ollama is not reachable on {self.ollama_service.endpoint}. "
                f"Install it from ollama.com and run: ollama pull {self.ollama_service.model}",
                type="warning",
            )
            return
        self.ai_explanation.content = f"_Asking local model {self.ollama_service.model}..._"
        self.ai_explanation.update()
        try:
            text = await nicegui_run.io_bound(
                self.ollama_service.explain_anomalies,
                report["source_label"],
                report["profile"],
                report["anomalies"],
            )
        except Exception as exc:
            self.ai_explanation.content = f"The local model could not produce an explanation: {exc}"
            self.ai_explanation.update()
            ui.notify(str(exc), type="negative")
            return
        self.ai_explanation.content = text or "The model returned an empty response."
        self.ai_explanation.update()
        self._set_last_action("Generated AI explanation for anomaly check")

    def _build_users_tab(self) -> None:
        with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-1"):
                    ui.label("WORKSPACE ACCESS").classes("dq-eyebrow")
                    ui.label("Accounts").classes("dq-panel-title text-2xl font-bold")
                    ui.label(
                        "Accounts are shared across the whole workspace. Only the Workspace Admin can manage them."
                    ).classes("dq-panel-copy text-sm")
                with ui.row().classes("items-center gap-2"):
                    ui.button("Add user", icon="person_add", on_click=lambda: self.show_user_dialog()).props(
                        "color=primary unelevated no-caps"
                    )
                    ui.button("Edit user", icon="manage_accounts", on_click=self.edit_selected_user).props(
                        "outline no-caps color=primary"
                    )
            self.users_table = self._build_table(["ID", "Username", "Workspace role", "Projects"])
            self.users_table.on("rowClick", self._select_user_row)
        with ui.card().classes("dq-soft-card dq-section-card w-full p-6 mt-4"):
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("PROJECT ACCESS").classes("dq-eyebrow")
                    self.members_title = ui.label("Project members").classes("dq-panel-title text-2xl font-bold")
                    ui.label(
                        "Pick a project you administer, then add existing accounts, change their role, or remove them."
                    ).classes("dq-panel-copy text-sm")
                with ui.row().classes("items-end gap-2 flex-wrap"):
                    self.member_project_select = ui.select(
                        options={}, label="Project", on_change=lambda _event: self._refresh_member_rows()
                    ).props("outlined dense").classes("min-w-[170px]")
                    self.member_user_select = ui.select(options=[], label="Account", with_input=True).props(
                        "outlined dense"
                    ).classes("min-w-[180px]")
                    self.member_role_select = ui.select(
                        {item.value: item.value for item in Role}, value=Role.USER.value, label="Project role"
                    ).props("outlined dense").classes("min-w-[140px]")
                    ui.button("Add / update", icon="person_add_alt", on_click=self.save_project_member).props(
                        "color=primary unelevated no-caps"
                    )
                    ui.button("Remove", icon="person_remove", on_click=self.remove_project_member).props(
                        "outline no-caps color=primary"
                    )
            self.members_table = self._build_table(["ID", "Username", "Project role"])
            self.members_table.on("rowClick", self._select_member_row)

    def _build_preview_tab(self) -> None:
        with ui.column().classes("w-full gap-4"):
            with ui.card().classes("dq-soft-card dq-section-card w-full p-6"):
                with ui.row().classes("w-full items-start justify-between gap-4 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("INSPECT").classes("dq-eyebrow")
                        ui.label("Source preview").classes("dq-panel-title text-2xl font-bold")
                        ui.label("Preview the first rows of a connection's file or table.").classes(
                            "dq-panel-copy text-sm"
                        )
                    with ui.row().classes("items-end gap-2 flex-wrap grow justify-end"):
                        self.preview_connection_select = ui.select(
                            options={}, label="Connection", on_change=self._load_preview_targets
                        ).props("outlined dense").classes("grow min-w-[190px] max-w-[280px]")
                        self.preview_target_select = ui.select(
                            options=[], label="File / table", with_input=True
                        ).props("outlined dense").classes("grow min-w-[190px] max-w-[320px]")
                        ui.button("Preview source", icon="visibility", on_click=self.preview_selected_connection_source).props(
                            "color=primary unelevated no-caps"
                        )
                self.preview_log = ui.markdown("Choose a connection and a file or table to preview.").classes("w-full")
            with ui.card().classes("dq-soft-card w-full p-5"):
                self.preview_table = self._build_table([], pagination=8)

    def sign_out(self) -> None:
        nicegui_app.storage.user.clear()
        ui.navigate.to("/login")

    def _populate_project_options(self) -> None:
        if not self.workspace or not self.signed_in:
            return
        projects = self.workspace.storage.projects_for_user(self.current_user)
        options = {str(project.id): project.name for project in projects}
        current = str(self.project_select.value) if self.project_select.value else None
        self.project_select.options = options
        self.project_select.value = current if current in options else (next(iter(options)) if options else None)
        self.project_select.update()
        if not options:
            self.workspace_hint.text = "You have no project access yet. Ask an Admin to add you to a project."

    def open_selected_project(self) -> None:
        if not self.workspace or not self.signed_in:
            ui.notify("Sign in first.", type="warning")
            return
        value = self.project_select.value
        if not value:
            ui.notify("Select a project first.", type="warning")
            return
        project_id = int(value)
        project_role = self.workspace.storage.role_in_project(self.current_user, project_id)
        if project_role is None:
            ui.notify("You do not have access to that project.", type="negative")
            self._populate_project_options()
            return
        record = self.workspace.storage.get_project(project_id)
        if record is None:
            ui.notify("That project no longer exists.", type="negative")
            self._populate_project_options()
            return
        try:
            path = self.workspace.project_dir(record)
            self.project = open_or_create_project(path)
            self.current_project = record
            self.current_role = project_role
            nicegui_app.storage.user["recent_project_id"] = project_id
            self.status_label.text = (
                f"Workspace: {self.workspace.root_dir.name} | Project: {record.name} | "
                f"User: {self.current_user} ({project_role.value})"
            )
            self.workspace_hint.text = "Project is writable and ready. Use the menu to manage data and run checks."
            self.refresh_all()
            self._set_last_action(f"Opened project {record.name}")
            ui.notify(f"Opened project: {record.name}", type="positive")
        except Exception as exc:
            self.workspace_hint.text = "The project folder could not be opened. Check that it is writable."
            ui.notify(str(exc), type="negative")

    def show_new_project_dialog(self) -> None:
        if not self.workspace or not self.signed_in:
            ui.notify("Sign in first.", type="warning")
            return
        if self.workspace_role != WorkspaceRole.WORKSPACE_ADMIN:
            ui.notify("Only the Workspace Admin can create projects.", type="warning")
            return
        usernames = [user.username for user in self.workspace.storage.list_users()]
        with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
            ui.label("NEW PROJECT").classes("dq-eyebrow")
            ui.label("Create a project").classes("dq-panel-title text-2xl font-bold")
            name = ui.input("Project name").classes("w-full")
            admins = ui.select(usernames, multiple=True, label="Project admins").props("outlined use-chips").classes("w-full")
            members = ui.select(usernames, multiple=True, label="Project users").props("outlined use-chips").classes("w-full")
            ui.label("Workspace Admins can always open every project.").classes("text-xs text-[#837d74]")

            def save() -> None:
                try:
                    project = self.workspace.create_project((name.value or "").strip(), created_by=self.current_user)
                except ValueError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                member_roles: dict[str, Role] = {username: Role.USER for username in (members.value or [])}
                member_roles.update({username: Role.ADMIN for username in (admins.value or [])})
                self.workspace.storage.set_project_members(project.id, member_roles)
                dialog.close()
                self._populate_project_options()
                self._populate_project_members()
                self.project_select.value = str(project.id)
                self.project_select.update()
                self._set_last_action(f"Created project {project.name}")
                ui.notify(f"Created project {project.name}.", type="positive")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Create", on_click=save).props("color=primary")
        dialog.open()

    def refresh_all(self) -> None:
        self._populate_dashboard()
        self._populate_connections()
        self._populate_rules_and_groups()
        self._populate_schedules()
        self._populate_results()
        self._populate_users()
        self._populate_project_members()

    def export_metadata(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        path_text = (self.export_path_input.value or "").strip()
        if not path_text:
            ui.notify("Enter a JSON path to export metadata.", type="warning")
            return
        path = Path(path_text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.project.storage.export_metadata(), indent=2), encoding="utf-8")
        self._set_last_action(f"Exported metadata to {path}")
        ui.notify(f"Exported metadata to {path}", type="positive")

    def import_metadata(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        path_text = (self.import_path_input.value or "").strip()
        if not path_text:
            ui.notify("Enter a JSON path to import metadata.", type="warning")
            return
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
        self.project.storage.import_metadata(payload)
        self.refresh_all()
        self._set_last_action(f"Imported metadata from {path_text}")
        ui.notify(f"Imported metadata from {path_text}", type="positive")

    def show_connection_dialog(self, existing: Connection | None = None) -> None:
        existing_config = existing.config if existing else {}
        with ui.dialog() as dialog, ui.card().classes("dq-scroll-dialog w-[760px] max-w-full"):
            ui.label("EDIT SOURCE" if existing else "NEW SOURCE").classes("dq-eyebrow")
            ui.label("Edit connection" if existing else "Create a connection").classes("dq-panel-title text-2xl font-bold")
            name = ui.input("Name", value=existing.name if existing else "").classes("w-full")
            connection_type = ui.select(
                {item.value: item.value for item in ConnectionType},
                value=existing.connection_type.value if existing else ConnectionType.CSV.value,
                label="Type",
            ).classes("w-full")
            visibility = ui.select(
                {"private": "private", "shared": "shared", "shared_specific": "shared_specific"},
                value=existing.visibility if existing else "private",
                label="Visibility",
            ).classes("w-full")
            allowed_users = ui.input(
                "Allowed Users (comma separated)",
                value=", ".join(existing.allowed_users) if existing else "",
            ).classes("w-full")

            async def browse_csv_file() -> None:
                if not self.project:
                    ui.notify("Open a project first.", type="warning")
                    return
                selected_path = await pick_server_path(
                    str(csv_file_path.value or self.project.uploads_dir),
                    directories_only=False,
                    extensions=(".csv",),
                    root=self.project.uploads_dir,
                )
                if selected_path is None:
                    return
                csv_file_path.value = str(selected_path)
                csv_file_path.update()
                selected_csv_label.text = f"Selected file: {selected_path.name}"
                selected_csv_label.update()
                if not (name.value or "").strip():
                    name.value = selected_path.stem
                    name.update()

            def finish_csv_upload(target_path: Path, content: bytes, original_name: str) -> None:
                with target_path.open("wb") as handle:
                    handle.write(content)
                csv_file_path.value = str(target_path)
                csv_file_path.update()
                selected_csv_label.text = f"Uploaded file: {target_path.name}"
                selected_csv_label.update()
                if not (name.value or "").strip():
                    name.value = target_path.stem
                    name.update()
                csv_uploader.reset()
                ui.notify(f"Uploaded {original_name} to the project's uploads folder.", type="positive")

            def confirm_overwrite(target_path: Path, content: bytes, original_name: str) -> None:
                with ui.dialog() as overwrite_dialog, ui.card().classes("w-[440px] max-w-full"):
                    ui.label("File already exists").classes("text-xl font-semibold")
                    ui.label(
                        f"'{original_name}' already exists in this project's uploads folder. Overwrite it with the file you just uploaded?"
                    ).classes("text-sm")

                    def do_overwrite() -> None:
                        overwrite_dialog.close()
                        finish_csv_upload(target_path, content, original_name)

                    def do_cancel() -> None:
                        overwrite_dialog.close()
                        csv_uploader.reset()
                        ui.notify("Upload canceled; the existing file was kept.", type="warning")

                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("Cancel", on_click=do_cancel).props("flat")
                        ui.button("Overwrite", icon="warning", on_click=do_overwrite).props(
                            "color=negative unelevated"
                        )
                overwrite_dialog.open()

            async def handle_csv_upload(event: events.UploadEventArguments) -> None:
                if not self.project:
                    ui.notify("Open a project first.", type="warning")
                    return
                # NiceGUI already strips directory components from event.file.name; Path(...).name
                # here is just defense in depth against a client sending something unexpected.
                safe_name = Path(event.file.name).name
                if not safe_name.lower().endswith(".csv"):
                    ui.notify("Only .csv files can be uploaded.", type="warning")
                    csv_uploader.reset()
                    return
                uploads_dir = self.project.uploads_dir
                uploads_dir.mkdir(parents=True, exist_ok=True)
                target_path = uploads_dir / safe_name
                content = await event.file.read()
                if target_path.exists():
                    confirm_overwrite(target_path, content, safe_name)
                else:
                    finish_csv_upload(target_path, content, safe_name)

            existing_csv_file = str(existing_config.get("file_path", "")) if existing else ""
            with ui.column().classes("w-full gap-1") as csv_picker:
                with ui.row().classes("w-full items-end gap-2"):
                    csv_file_path = ui.input(
                        "CSV file",
                        value=existing_csv_file,
                        placeholder=r"C:\Data\customers.csv",
                    ).props("outlined").classes("grow")
                    ui.button("Browse CSV", icon="folder_open", on_click=browse_csv_file).props(
                        "color=primary unelevated no-caps"
                    )
                ui.label("Browse only shows files already in this project's uploads folder.").classes(
                    "text-xs text-[#837d74]"
                )
                csv_uploader = ui.upload(
                    label="Or upload a CSV from your computer (saved into the project's uploads folder)",
                    on_upload=handle_csv_upload,
                    auto_upload=True,
                    max_files=1,
                    max_file_size=200_000_000,
                ).props('accept=".csv" flat bordered').classes("w-full")
                selected_csv_label = ui.label(
                    f"Selected file: {Path(existing_csv_file).name}" if existing_csv_file else "No CSV file selected."
                ).classes("text-xs text-[#837d74]")
            host = ui.input("Host", value=str(existing_config.get("host", ""))).classes("w-full")
            port = ui.number("Port", value=existing_config.get("port", 1521), format="%.0f").classes("w-full")
            service_name = ui.input("Service Name", value=str(existing_config.get("service_name", ""))).classes("w-full")
            database_name = ui.input("Database", value=str(existing_config.get("database", ""))).classes("w-full")
            # Defaults come from the connector layer so the dialog can never drift from
            # what connect_database() actually uses.
            default_drivers = {
                odbc_type.value: settings.default_driver for odbc_type, settings in ODBC_SETTINGS.items()
            }
            initial_driver = str(existing_config.get("driver", "")) or default_drivers.get(
                existing.connection_type.value if existing else "", "ODBC Driver 17 for SQL Server"
            )
            odbc_driver = ui.input("ODBC Driver", value=initial_driver).classes("w-full")
            username = ui.input("Username", value=str(existing_config.get("username", ""))).classes("w-full")
            tns_alias = ui.input("TNS Alias", value=str(existing_config.get("tns_alias", ""))).classes("w-full")
            password = ui.input(
                "Password (saved locally)",
                placeholder="Leave blank to keep the saved password" if existing else "",
            ).props("type=password").classes("w-full")

            default_ports = {
                ConnectionType.ORACLE.value: 1521,
                **{odbc_type.value: settings.default_port for odbc_type, settings in ODBC_SETTINGS.items()},
            }

            def sync_visibility() -> None:
                selected = str(connection_type.value)
                is_database = selected != ConnectionType.CSV.value
                is_oracle = selected == ConnectionType.ORACLE.value
                csv_picker.visible = not is_database
                host.visible = is_database
                port.visible = is_database
                username.visible = is_database
                password.visible = is_database
                service_name.visible = is_oracle
                tns_alias.visible = is_oracle
                database_name.visible = selected in default_drivers
                odbc_driver.visible = selected in default_drivers
                if is_database and port.value in (None, *default_ports.values()):
                    port.value = default_ports.get(selected, 1521)
                if selected in default_drivers and str(odbc_driver.value or "").strip() in ("", *default_drivers.values()):
                    odbc_driver.value = default_drivers[selected]
                allowed_users.visible = visibility.value == "shared_specific"
                for element in (
                    csv_picker, host, port, service_name, database_name, odbc_driver,
                    username, tns_alias, password, allowed_users,
                ):
                    element.update()

            connection_type.on_value_change(lambda _event: sync_visibility())
            visibility.on_value_change(lambda _event: sync_visibility())
            sync_visibility()

            def save() -> None:
                try:
                    selected_type = ConnectionType(connection_type.value)
                    selected_csv_path = Path(str(csv_file_path.value or "").strip())
                    config = {
                        "file_path": str(selected_csv_path),
                        "base_path": str(selected_csv_path.parent),
                    }
                    if selected_type == ConnectionType.ORACLE:
                        config = {
                            "host": (host.value or "").strip(),
                            "port": int(port.value or 1521),
                            "service_name": (service_name.value or "").strip(),
                            "username": (username.value or "").strip(),
                            "tns_alias": (tns_alias.value or "").strip(),
                        }
                    elif selected_type in ODBC_SETTINGS:
                        config = {
                            "host": (host.value or "").strip(),
                            "port": int(port.value or default_ports[selected_type.value]),
                            "database": (database_name.value or "").strip(),
                            "username": (username.value or "").strip(),
                            "driver": (odbc_driver.value or "").strip() or default_drivers[selected_type.value],
                        }
                    connection = Connection(
                        id=existing.id if existing else None,
                        name=(name.value or "").strip(),
                        connection_type=selected_type,
                        owner_username=existing.owner_username if existing else self.current_user,
                        visibility=str(visibility.value),
                        allowed_users=self._split_csv_text(allowed_users.value),
                        config=config,
                        tags=[],
                    )
                    if not connection.name:
                        raise ValueError("Connection name is required.")
                    if selected_type == ConnectionType.CSV:
                        if not str(csv_file_path.value or "").strip():
                            raise ValueError("Select a CSV file.")
                        if selected_csv_path.suffix.lower() != ".csv":
                            raise ValueError("The selected connection file must have a .csv extension.")
                        if not selected_csv_path.is_file():
                            raise ValueError(f"CSV file does not exist: {selected_csv_path}")
                    connection_id = self.project.storage.save_connection(connection) if self.project else None
                    if selected_type != ConnectionType.CSV:
                        if password.value:
                            save_connection_secret(connection.name, config.get("username", ""), str(password.value))
                        elif existing:
                            # Secrets are keyed by username:connection name; keep them reachable after a rename.
                            old_username = str(existing.config.get("username", ""))
                            new_username = config.get("username", "")
                            if (existing.name, old_username) != (connection.name, new_username):
                                old_secret = get_connection_secret(existing.name, old_username)
                                if old_secret:
                                    save_connection_secret(connection.name, new_username, old_secret)
                    dialog.close()
                    self.refresh_all()
                    self._set_last_action(f"Saved connection {connection.name}")
                    ui.notify(f"Saved connection #{connection_id}: {connection.name}", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("color=primary")
        dialog.open()

    def show_rule_dialog(self, rule: Rule | None = None) -> None:
        if not self.project:
            ui.notify("Open a project before creating a rule.", type="warning")
            return
        connections = {str(item.id): item for item in self._visible_connections() if item.id is not None}
        if not connections:
            ui.notify("Create an accessible connection before creating a rule.", type="warning")
            return
        connection_options = {
            key: f"{item.name} · {item.connection_type.value.upper()}"
            for key, item in connections.items()
        }
        default_connection = next(iter(connections)) if len(connections) == 1 else None
        with ui.dialog() as dialog, ui.card().classes("dq-rule-dialog w-[980px] max-w-full"):
            ui.label("Edit Rule" if rule else "Rule").classes("text-xl font-semibold")
            name = ui.input("Name", value=rule.name if rule else "").classes("w-full")
            description = ui.textarea(
                "Description (optional)", value=rule.description if rule else ""
            ).props("outlined autogrow").classes("w-full")
            rule_type = ui.select(
                {item.value: RULE_TEMPLATES[item]["name"] for item in RuleType},
                value=rule.rule_type.value if rule else RuleType.NOT_NULL.value,
                label="Type",
            ).classes("w-full")
            visibility = ui.select(
                {"private": "private", "shared": "shared", "shared_specific": "shared_specific"},
                value=rule.visibility if rule else "private",
                label="Visibility",
            ).classes("w-full")
            allowed_users = ui.input(
                "Allowed Users (comma separated)",
                value=", ".join(rule.allowed_users) if rule else "",
            ).classes("w-full")
            hint = ui.markdown("").classes("w-full")

            with ui.column().classes("dq-reference-panel gap-3"):
                with ui.row().classes("items-center gap-3"):
                    with ui.element("div").classes("grid w-10 h-10 rounded-xl bg-stone-200 place-items-center"):
                        ui.icon("percent", color="warning").classes("text-xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Failure tolerance").classes("dq-panel-title text-lg font-bold")
                        ui.label("Optional: allow a few failed rows before this run counts as failed.").classes(
                            "text-xs text-[#837d74]"
                        )
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    fail_threshold_count = ui.number(
                        "Fail threshold (rows)",
                        value=int(rule.config.get("fail_threshold_count", 0) or 0) if rule else 0,
                        format="%.0f",
                    ).classes("grow min-w-[180px]")
                    fail_threshold_percent = ui.number(
                        "Fail threshold (%)",
                        value=float(rule.config.get("fail_threshold_percent", 0) or 0) if rule else 0,
                    ).classes("grow min-w-[180px]")
                ui.label(
                    "The run still passes when failed rows stay at or below whichever allowance is larger. "
                    "Leave both at 0 to fail on any failed row (default)."
                ).classes("dq-panel-copy text-xs")

            with ui.column().classes("dq-reference-panel gap-3"):
                with ui.row().classes("items-center gap-3"):
                    with ui.element("div").classes("grid w-10 h-10 rounded-xl bg-stone-200 place-items-center"):
                        ui.icon("storage", color="primary").classes("text-xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Source").classes("dq-panel-title text-lg font-bold")
                        ui.label(f"Required - {len(connections)} connection(s) available").classes("text-xs text-[#837d74]")
                source_connection = ui.select(
                    connection_options,
                    value=default_connection,
                    label="Source connection *",
                    clearable=True,
                ).props("outlined options-dense").classes("w-full")
                source_kind = ui.select(
                    {"csv_file": "CSV File", "oracle_table": "Table / View", "oracle_sql": "Custom SQL"},
                    label="Source type *",
                ).props("outlined options-dense").classes("w-full")
                source_name = ui.select(
                    [],
                    label="CSV file / table *",
                    with_input=True,
                    new_value_mode="add-unique",
                    clearable=True,
                ).props("outlined options-dense").classes("w-full")
                source_sql = ui.textarea("Source SQL *").classes("w-full")
                source_targets = ui.label("Select a connection to load its files or tables.").classes(
                    "text-xs text-[#837d74]"
                )
                source_columns = ui.markdown("").classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("Reload files / tables", icon="refresh", on_click=lambda: refresh_source_targets()).props(
                        "outline no-caps"
                    )
                    ui.button(
                        "List Source Columns",
                        on_click=lambda: refresh_source_rule_columns(),
                    )

            with ui.column().classes("dq-reference-panel gap-3") as target_helper:
                with ui.row().classes("items-center gap-3"):
                    with ui.element("div").classes("grid w-10 h-10 rounded-xl bg-stone-200 place-items-center"):
                        ui.icon("ads_click", color="secondary").classes("text-xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Target").classes("dq-panel-title text-lg font-bold")
                        ui.label("Required for referential integrity rules").classes("text-xs text-[#837d74]")
                target_connection = ui.select(
                    connection_options,
                    value=default_connection,
                    label="Target connection *",
                    clearable=True,
                ).props("outlined options-dense").classes("w-full")
                target_kind = ui.select(
                    {"csv_file": "CSV File", "oracle_table": "Table / View", "oracle_sql": "Custom SQL"},
                    label="Target type *",
                ).props("outlined options-dense").classes("w-full")
                target_name = ui.select(
                    [],
                    label="CSV file / table *",
                    with_input=True,
                    new_value_mode="add-unique",
                    clearable=True,
                ).props("outlined options-dense").classes("w-full")
                target_sql = ui.textarea("Target SQL *").classes("w-full")
                target_status = ui.label("Select a connection to load its files or tables.").classes(
                    "text-xs text-[#837d74]"
                )
                target_columns = ui.markdown("").classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("Reload files / tables", icon="refresh", on_click=lambda: refresh_target_targets()).props(
                        "outline no-caps"
                    )
                    ui.button(
                        "List Target Columns",
                        on_click=lambda: refresh_target_rule_columns(),
                    )

            with ui.column().classes("dq-reference-panel gap-3"):
                with ui.row().classes("items-center gap-3"):
                    with ui.element("div").classes("grid w-10 h-10 rounded-xl bg-stone-200 place-items-center"):
                        ui.icon("tune", color="grey-8").classes("text-xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Rule settings").classes("dq-panel-title text-lg font-bold")
                        ui.label("Choose what this rule should check").classes("text-xs text-[#837d74]")

                field_select = ui.select(
                    [], label="Field *", with_input=True, clearable=True
                ).props("outlined options-dense").classes("w-full")
                fields_select = ui.select(
                    [], label="Fields *", with_input=True, multiple=True
                ).props("outlined use-chips options-dense").classes("w-full")
                with ui.row().classes("w-full gap-3 flex-wrap") as row_count_fields:
                    min_count = ui.number("Minimum rows", value=1, format="%.0f").classes("grow min-w-[180px]")
                    max_count = ui.number("Maximum rows", value=100000, format="%.0f").classes("grow min-w-[180px]")
                with ui.row().classes("w-full gap-3 flex-wrap") as value_range_fields:
                    min_value = ui.number("Minimum value", value=0).classes("grow min-w-[180px]")
                    max_value = ui.number("Maximum value", value=1000).classes("grow min-w-[180px]")
                regex_pattern = ui.input("Regular expression *", placeholder=r"^[^@]+@[^@]+$").classes("w-full")
                with ui.row().classes("w-full gap-3 flex-wrap") as length_fields:
                    min_length = ui.number("Minimum length", value=1, format="%.0f").classes("grow min-w-[180px]")
                    max_length = ui.number("Maximum length", value=20, format="%.0f").classes("grow min-w-[180px]")
                allowed_values = ui.input("Allowed values *", placeholder="ACTIVE, INACTIVE").classes("w-full")
                rule_sql = ui.textarea("Rule SQL *").props("autogrow").classes("w-full font-mono")
                with ui.row().classes("w-full gap-3 flex-wrap") as threshold_fields:
                    threshold_operator = ui.select(
                        {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "==", "!=": "!="},
                        value=">",
                        label="Comparison",
                    ).classes("grow min-w-[180px]")
                    threshold_value = ui.number("Threshold", value=0).classes("grow min-w-[180px]")
                target_key_select = ui.select(
                    [], label="Target key field *", with_input=True, clearable=True
                ).props("outlined options-dense").classes("w-full")
                target_relation = ui.input("Target relation *").classes("w-full")

            with ui.expansion("Advanced configuration JSON", value=False).classes("w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Advanced settings").classes("text-sm font-medium")
                    ui.button("Reset to example", on_click=lambda: set_example()).props("outline dense no-caps")
                ui.label("The form above overrides matching values in this JSON when saved.").classes(
                    "text-xs text-[#837d74]"
                )
                config_json = ui.textarea("").props("autogrow outlined").classes("w-full font-mono")

            def apply_setting_values(config: dict[str, Any], *, preserve_unknown_fields: bool = True) -> None:
                selected_type = RuleType(rule_type.value)
                single_value = config.get("source_key") if selected_type == RuleType.REFERENTIAL_INTEGRITY else config.get("key_column") if selected_type == RuleType.KEYED_COMPARISON else config.get("column")
                if preserve_unknown_fields and single_value and single_value not in field_select.options:
                    field_select.options = [single_value, *field_select.options]
                configured_fields = list(config.get("compare_columns") or config.get("columns") or [])
                if preserve_unknown_fields:
                    fields_select.options = [
                        *[value for value in configured_fields if value not in fields_select.options],
                        *fields_select.options,
                    ]
                target_key = config.get("target_key")
                if preserve_unknown_fields and target_key and target_key not in target_key_select.options:
                    target_key_select.options = [target_key, *target_key_select.options]
                field_select.value = single_value if single_value in field_select.options else None
                fields_select.value = [value for value in configured_fields if value in fields_select.options]
                min_count.value = config.get("min_count", 1)
                max_count.value = config.get("max_count", 100000)
                min_value.value = config.get("min", 0)
                max_value.value = config.get("max", 1000)
                regex_pattern.value = config.get("pattern", "")
                min_length.value = config.get("min_length", 1)
                max_length.value = config.get("max_length", 20)
                allowed_values.value = ", ".join(str(value) for value in config.get("values", []))
                rule_sql.value = config.get("sql", "")
                threshold_operator.value = config.get("operator", ">")
                threshold_value.value = config.get("threshold", 0)
                target_key_select.value = target_key if target_key in target_key_select.options else None
                target_relation.value = config.get("target_relation", "")
                for element in (
                    field_select, fields_select, min_count, max_count, min_value, max_value,
                    regex_pattern, min_length, max_length, allowed_values, rule_sql,
                    threshold_operator, threshold_value, target_key_select, target_relation,
                ):
                    element.update()

            def update_setting_visibility() -> None:
                selected_type = RuleType(rule_type.value)
                single_field_types = {
                    RuleType.NOT_NULL,
                    RuleType.VALUE_RANGE,
                    RuleType.REGEX,
                    RuleType.LENGTH,
                    RuleType.ALLOWED_VALUES,
                    RuleType.DATE_VALIDITY,
                    RuleType.REFERENTIAL_INTEGRITY,
                    RuleType.KEYED_COMPARISON,
                }
                field_select.visible = selected_type in single_field_types
                fields_select.visible = selected_type in {RuleType.UNIQUE, RuleType.DUPLICATE, RuleType.KEYED_COMPARISON}
                row_count_fields.visible = selected_type == RuleType.ROW_COUNT
                value_range_fields.visible = selected_type == RuleType.VALUE_RANGE
                regex_pattern.visible = selected_type == RuleType.REGEX
                length_fields.visible = selected_type == RuleType.LENGTH
                allowed_values.visible = selected_type == RuleType.ALLOWED_VALUES
                rule_sql.visible = selected_type in {
                    RuleType.CUSTOM_SQL_FAIL_ROWS,
                    RuleType.CUSTOM_SQL_THRESHOLD,
                    RuleType.CUSTOM_SQL_CONNECTION,
                }
                threshold_fields.visible = selected_type == RuleType.CUSTOM_SQL_THRESHOLD
                target_key_select.visible = selected_type == RuleType.REFERENTIAL_INTEGRITY
                target_relation.visible = selected_type == RuleType.KEYED_COMPARISON

                field_label = "Field *"
                if selected_type == RuleType.REFERENTIAL_INTEGRITY:
                    field_label = "Source key field *"
                elif selected_type == RuleType.KEYED_COMPARISON:
                    field_label = "Key field *"
                field_select.props["label"] = field_label
                fields_select.props["label"] = "Comparison fields *" if selected_type == RuleType.KEYED_COMPARISON else "Fields *"
                for element in (
                    field_select, fields_select, row_count_fields, value_range_fields, regex_pattern,
                    length_fields, allowed_values, rule_sql, threshold_fields,
                    target_key_select, target_relation,
                ):
                    element.update()

            def set_example() -> None:
                selected_type = RuleType(rule_type.value)
                example = dict(RULE_CONFIG_EXAMPLES[selected_type])
                config_json.value = json.dumps(example, indent=2)
                config_json.update()
                apply_setting_values(example, preserve_unknown_fields=False)
                update_hint()

            def update_hint() -> None:
                selected_type = RuleType(rule_type.value)
                template = RULE_TEMPLATES[selected_type]
                hint.content = (
                    f"**What it does**  \n{template['description']}\n\n"
                    f"**How to set it up**  \n{template['setup']}"
                )
                hint.update()
                allowed_users.visible = visibility.value == "shared_specific"
                allowed_users.update()
                target_helper.visible = selected_type == RuleType.REFERENTIAL_INTEGRITY
                target_helper.update()
                update_setting_visibility()

            def sync_reference_fields(
                connection_select: Any,
                kind_select: ui.select,
                name_field: ui.select,
                sql_field: ui.textarea,
            ) -> None:
                connection = connections.get(str(connection_select.value))
                is_connection_rule = (
                    kind_select is source_kind and RuleType(rule_type.value) == RuleType.CUSTOM_SQL_CONNECTION
                )
                if connection is None:
                    options: dict[str, str] = {}
                elif is_connection_rule:
                    options = {"connection": "Whole connection"}
                elif connection.connection_type == ConnectionType.CSV:
                    options = {"csv_file": "CSV File"}
                else:
                    options = {
                        "oracle_table": "Table / View",
                        "oracle_sql": "Custom SQL",
                    }
                current_kind = str(kind_select.value or "")
                kind_select.options = options
                kind_select.value = current_kind if current_kind in options else (next(iter(options), None))
                kind_select.visible = (
                    connection is not None
                    and connection.connection_type != ConnectionType.CSV
                    and not is_connection_rule
                )
                kind_select.update()
                uses_sql = kind_select.value == "oracle_sql"
                uses_single_csv_file = (
                    connection is not None
                    and connection.connection_type == ConnectionType.CSV
                    and self.connector_service.csv_connection_file(connection) is not None
                )
                name_field.visible = (
                    bool(kind_select.value) and not uses_sql and not uses_single_csv_file and not is_connection_rule
                )
                sql_field.visible = uses_sql
                if connection is None:
                    name_field.props["label"] = "CSV file / table *"
                elif connection.connection_type == ConnectionType.CSV:
                    name_field.props["label"] = "CSV file *"
                else:
                    type_label = CONNECTION_TYPE_LABELS.get(connection.connection_type.value, connection.connection_type.value)
                    name_field.props["label"] = f"{type_label} table / view *"
                name_field.update()
                sql_field.update()

            async def populate_targets(
                connection_select: ui.select,
                kind_select: ui.select,
                name_select: ui.select,
                status_label: ui.label,
            ) -> None:
                connection = connections.get(str(connection_select.value))
                source_kind_value = str(kind_select.value or "")
                current_value = str(name_select.value or "").strip()
                if connection is None:
                    name_select.options = []
                    name_select.value = None
                    status_label.text = "Select a connection to load its files or tables."
                elif source_kind_value == "oracle_sql":
                    name_select.options = []
                    name_select.value = None
                    status_label.text = "Enter the SQL query in the field below."
                elif source_kind_value == "connection":
                    name_select.options = []
                    name_select.value = None
                    status_label.text = "Loading what the SQL can reference..."
                    status_label.update()
                    try:
                        targets = await nicegui_run.io_bound(self.connector_service.list_connection_targets, connection)
                    except Exception as exc:
                        status_label.text = f"The SQL runs against the whole connection. Could not list its items: {exc}"
                    else:
                        if connection.connection_type == ConnectionType.CSV:
                            names = []
                            for target in targets:
                                stem = target.rsplit("/", 1)[-1]
                                stem = stem[:-4] if stem.lower().endswith(".csv") else stem
                                cleaned = re.sub(r"\W+", "_", stem).strip("_") or "csv"
                                names.append(f"t_{cleaned}" if cleaned[0].isdigit() else cleaned)
                        else:
                            names = targets
                        preview = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
                        status_label.text = (
                            f"The SQL can reference: {preview}" if names else "No tables or files were found on this connection."
                        )
                else:
                    selected_csv_file = (
                        self.connector_service.csv_connection_file(connection)
                        if connection.connection_type == ConnectionType.CSV
                        else None
                    )
                    if selected_csv_file is not None:
                        name_select.options = [selected_csv_file.name]
                        name_select.value = selected_csv_file.name
                        status_label.text = f"Using CSV file: {selected_csv_file.name}"
                        name_select.update()
                        status_label.update()
                        return
                    status_label.text = "Loading available CSV files..." if connection.connection_type == ConnectionType.CSV else "Loading tables and views..."
                    status_label.update()
                    try:
                        targets = await nicegui_run.io_bound(self.connector_service.list_connection_targets, connection)
                    except Exception as exc:
                        targets = []
                        status_label.text = f"Could not load available items: {exc}"
                    else:
                        noun = "CSV file" if connection.connection_type == ConnectionType.CSV else "table or view"
                        status_label.text = f"Found {len(targets)} {noun}{'' if len(targets) == 1 else 's'}."
                    if current_value and current_value not in targets:
                        targets = [current_value, *targets]
                    name_select.options = targets
                    name_select.value = current_value if current_value in targets else None
                name_select.update()
                status_label.update()

            async def populate_rule_columns(
                connection_select: ui.select,
                kind_select: ui.select,
                name_select: ui.select,
                sql_input: ui.textarea,
                output: ui.markdown,
                column_selects: tuple[ui.select, ...],
            ) -> None:
                connection_id = connection_select.value
                if not connection_id or not kind_select.value:
                    output.content = "Select a connection first."
                    output.update()
                    return
                if kind_select.value == "connection":
                    output.content = "Whole-connection rules take their fields from the SQL itself."
                    output.update()
                    return
                config = {
                    "source_connection_id": int(connection_id),
                    "source_kind": kind_select.value,
                    "source_name": str(name_select.value or "").strip(),
                    "source_sql": str(sql_input.value or "").strip(),
                }
                connection_lookup = {
                    item.id: item for item in self._visible_connections() if item.id is not None
                }
                try:
                    columns = await nicegui_run.io_bound(
                        self.connector_service.list_rule_source_columns,
                        config,
                        connection_lookup,
                    )
                except Exception as exc:
                    output.content = f"Could not load fields: {exc}"
                    output.update()
                    return
                cleared_values: list[str] = []
                for column_select in column_selects:
                    current = column_select.value
                    if isinstance(current, list):
                        invalid = [value for value in current if value not in columns]
                        cleared_values.extend(str(value) for value in invalid)
                        column_select.value = [value for value in current if value in columns]
                    elif current not in (None, "") and current not in columns:
                        cleared_values.append(str(current))
                        column_select.value = None
                    column_select.options = columns
                    column_select.update()
                output.content = f"Loaded **{len(columns)}** fields from the selected source."
                if cleared_values:
                    output.content += f" Removed unavailable selection(s): {', '.join(cleared_values)}."
                output.update()

            async def refresh_source_rule_columns() -> None:
                await populate_rule_columns(
                    source_connection,
                    source_kind,
                    source_name,
                    source_sql,
                    source_columns,
                    (field_select, fields_select),
                )

            async def refresh_target_rule_columns() -> None:
                await populate_rule_columns(
                    target_connection,
                    target_kind,
                    target_name,
                    target_sql,
                    target_columns,
                    (target_key_select,),
                )

            async def refresh_source_targets() -> None:
                sync_reference_fields(source_connection, source_kind, source_name, source_sql)
                await populate_targets(source_connection, source_kind, source_name, source_targets)
                if source_connection.value and source_kind.value and (source_name.value or source_sql.value):
                    await refresh_source_rule_columns()

            async def refresh_target_targets() -> None:
                sync_reference_fields(target_connection, target_kind, target_name, target_sql)
                await populate_targets(target_connection, target_kind, target_name, target_status)
                if target_connection.value and target_kind.value and (target_name.value or target_sql.value):
                    await refresh_target_rule_columns()

            async def handle_rule_type_change() -> None:
                set_example()
                await refresh_source_targets()
                if RuleType(rule_type.value) == RuleType.REFERENTIAL_INTEGRITY:
                    await refresh_target_targets()

            rule_type.on_value_change(lambda _event: handle_rule_type_change())
            visibility.on_value_change(lambda _event: update_hint())
            source_connection.on_value_change(lambda _event: refresh_source_targets())
            source_kind.on_value_change(lambda _event: refresh_source_targets())
            source_name.on_value_change(lambda _event: refresh_source_rule_columns())
            target_connection.on_value_change(lambda _event: refresh_target_targets())
            target_kind.on_value_change(lambda _event: refresh_target_targets())
            target_name.on_value_change(lambda _event: refresh_target_rule_columns())

            existing_config = normalize_rule_config(rule.rule_type, rule.config) if rule else None
            if existing_config:
                source_connection.value = self._id_to_str(existing_config.get("source_connection_id"))
                source_kind.value = existing_config.get("source_kind") or source_kind.value
                existing_source_name = str(existing_config.get("source_name", ""))
                source_name.options = [existing_source_name] if existing_source_name else []
                source_name.value = existing_source_name or None
                source_sql.value = str(existing_config.get("source_sql", ""))
                target_connection.value = self._id_to_str(existing_config.get("target_connection_id"))
                target_kind.value = existing_config.get("target_kind") or target_kind.value
                existing_target_name = str(existing_config.get("target_name", ""))
                target_name.options = [existing_target_name] if existing_target_name else []
                target_name.value = existing_target_name or None
                target_sql.value = str(existing_config.get("target_sql", ""))
                config_json.value = json.dumps(existing_config, indent=2)
                apply_setting_values(existing_config)
            else:
                set_example()
            sync_reference_fields(source_connection, source_kind, source_name, source_sql)
            sync_reference_fields(target_connection, target_kind, target_name, target_sql)
            update_hint()
            ui.timer(0.1, refresh_source_targets, once=True)
            if RuleType(rule_type.value) == RuleType.REFERENTIAL_INTEGRITY:
                ui.timer(0.1, refresh_target_targets, once=True)

            def save() -> None:
                try:
                    if not self.project:
                        raise ValueError("Open a project first.")
                    if not (name.value or "").strip():
                        raise ValueError("Rule name is required.")
                    selected_type = RuleType(rule_type.value)
                    parsed_config = json.loads((config_json.value or "{}").strip() or "{}")
                    if not isinstance(parsed_config, dict):
                        raise ValueError("Rule config JSON must be an object.")

                    merged = dict(parsed_config)
                    for key in ("source_connection_id", "source_kind", "source_name", "source_sql"):
                        merged.pop(key, None)
                    if not source_connection.value or str(source_connection.value) not in connections:
                        raise ValueError("Select a valid source connection.")
                    merged["source_connection_id"] = int(source_connection.value)
                    merged["source_kind"] = source_kind.value
                    merged["source_name"] = str(source_name.value or "").strip()
                    merged["source_sql"] = str(source_sql.value or "").strip()

                    for key in ("target_connection_id", "target_kind", "target_name", "target_sql"):
                        merged.pop(key, None)
                    if selected_type == RuleType.REFERENTIAL_INTEGRITY:
                        if not target_connection.value or str(target_connection.value) not in connections:
                            raise ValueError("Select a valid target connection for this referential integrity rule.")
                        merged["target_connection_id"] = int(target_connection.value)
                        merged["target_kind"] = target_kind.value
                        merged["target_name"] = str(target_name.value or "").strip()
                        merged["target_sql"] = str(target_sql.value or "").strip()

                    setting_keys = {
                        "column", "columns", "min_count", "max_count", "min", "max", "pattern",
                        "min_length", "max_length", "values", "sql", "operator", "threshold",
                        "source_key", "target_key", "key_column", "compare_columns", "target_relation",
                        "fail_threshold_count", "fail_threshold_percent",
                    }
                    for key in setting_keys:
                        merged.pop(key, None)
                    merged["fail_threshold_count"] = int(fail_threshold_count.value or 0)
                    merged["fail_threshold_percent"] = float(fail_threshold_percent.value or 0)

                    if selected_type in {
                        RuleType.NOT_NULL,
                        RuleType.VALUE_RANGE,
                        RuleType.REGEX,
                        RuleType.LENGTH,
                        RuleType.ALLOWED_VALUES,
                        RuleType.DATE_VALIDITY,
                    }:
                        merged["column"] = str(field_select.value or "").strip()
                    if selected_type in {RuleType.UNIQUE, RuleType.DUPLICATE}:
                        merged["columns"] = list(fields_select.value or [])
                    elif selected_type == RuleType.ROW_COUNT:
                        merged["min_count"] = int(min_count.value or 0)
                        merged["max_count"] = int(max_count.value or 0)
                    elif selected_type == RuleType.VALUE_RANGE:
                        merged["min"] = min_value.value
                        merged["max"] = max_value.value
                    elif selected_type == RuleType.REGEX:
                        merged["pattern"] = str(regex_pattern.value or "").strip()
                    elif selected_type == RuleType.LENGTH:
                        merged["min_length"] = int(min_length.value or 0)
                        merged["max_length"] = int(max_length.value or 0)
                    elif selected_type == RuleType.ALLOWED_VALUES:
                        merged["values"] = self._split_csv_text(allowed_values.value)
                    elif selected_type in {RuleType.CUSTOM_SQL_FAIL_ROWS, RuleType.CUSTOM_SQL_CONNECTION}:
                        merged["sql"] = str(rule_sql.value or "").strip()
                    elif selected_type == RuleType.CUSTOM_SQL_THRESHOLD:
                        merged["sql"] = str(rule_sql.value or "").strip()
                        merged["operator"] = str(threshold_operator.value or ">")
                        merged["threshold"] = threshold_value.value
                    elif selected_type == RuleType.REFERENTIAL_INTEGRITY:
                        merged["source_key"] = str(field_select.value or "").strip()
                        merged["target_key"] = str(target_key_select.value or "").strip()
                    elif selected_type == RuleType.KEYED_COMPARISON:
                        merged["key_column"] = str(field_select.value or "").strip()
                        merged["compare_columns"] = list(fields_select.value or [])
                        merged["target_relation"] = str(target_relation.value or "").strip()

                    normalized = normalize_rule_config(selected_type, merged)
                    errors = validate_rule_config(selected_type, normalized, require_source=True)
                    if errors:
                        raise ValueError("\n".join(errors))

                    new_rule = Rule(
                        id=rule.id if rule else None,
                        name=(name.value or "").strip(),
                        rule_type=selected_type,
                        dataset_id=None,
                        owner_username=rule.owner_username if rule else self.current_user,
                        description=str(description.value or "").strip(),
                        visibility=str(visibility.value),
                        allowed_users=self._split_csv_text(allowed_users.value),
                        config=normalized,
                        tags=[],
                    )
                    rule_id = self.project.storage.save_rule(new_rule)
                    dialog.close()
                    self.refresh_all()
                    self._set_last_action(f"Saved rule {new_rule.name}")
                    ui.notify(f"Saved rule #{rule_id}: {new_rule.name}", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("color=primary")
        dialog.open()

    def show_user_dialog(self, existing_username: str | None = None) -> None:
        if not self.workspace or not self.signed_in or self.workspace_role != WorkspaceRole.WORKSPACE_ADMIN:
            ui.notify("Only the Workspace Admin can manage accounts.", type="warning")
            return
        storage = self.workspace.storage
        existing = storage.get_user(existing_username) if existing_username else None
        projects = storage.list_projects()
        project_options = {str(project.id): project.name for project in projects}
        admin_of: list[str] = []
        user_of: list[str] = []
        if existing:
            for project in projects:
                member_role = storage.list_project_members(project.id).get(existing.username)
                if member_role == Role.ADMIN:
                    admin_of.append(str(project.id))
                elif member_role == Role.USER:
                    user_of.append(str(project.id))
        with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
            ui.label("EDIT ACCOUNT" if existing else "NEW ACCOUNT").classes("dq-eyebrow")
            ui.label("Edit user" if existing else "Create a user").classes("dq-panel-title text-2xl font-bold")
            username = ui.input("Username", value=existing.username if existing else "").classes("w-full")
            if existing:
                username.props("readonly")
            role = ui.select(
                {item.value: item.value for item in WorkspaceRole},
                value=(existing.role.value if existing else WorkspaceRole.MEMBER.value),
                label="Workspace role",
            ).classes("w-full")
            password = ui.input(
                "Password (leave empty to keep current)" if existing else "Password",
                password=True,
                password_toggle_button=True,
            ).classes("w-full")
            admin_select = ui.select(project_options, multiple=True, value=admin_of, label="Project admin of").props(
                "outlined use-chips"
            ).classes("w-full")
            user_select = ui.select(project_options, multiple=True, value=user_of, label="Project user of").props(
                "outlined use-chips"
            ).classes("w-full")
            ui.label("A Workspace Admin can open every project as admin regardless of these lists.").classes(
                "text-xs text-[#837d74]"
            )

            def save() -> None:
                value = (username.value or "").strip()
                if not value:
                    ui.notify("Username is required.", type="warning")
                    return
                try:
                    storage.upsert_user(value, WorkspaceRole(role.value), password=(password.value or None))
                except ValueError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                admin_ids = {int(item) for item in (admin_select.value or [])}
                user_ids = {int(item) for item in (user_select.value or [])}
                for project in projects:
                    if project.id in admin_ids:
                        storage.set_project_member(project.id, value, Role.ADMIN)
                    elif project.id in user_ids:
                        storage.set_project_member(project.id, value, Role.USER)
                    else:
                        storage.remove_project_member(project.id, value)
                dialog.close()
                self._populate_users()
                self._populate_project_options()
                self._populate_project_members()
                self._set_last_action(f"Saved user {value}")
                ui.notify(f"Saved user {value} ({role.value}).", type="positive")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("color=primary")
        dialog.open()

    def edit_selected_rule(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        rule = self._selected_rule()
        if rule is None:
            ui.notify("Select a rule first.", type="warning")
            return
        if self.current_role != Role.ADMIN and rule.owner_username != self.current_user:
            ui.notify("You can only edit rules that you own.", type="warning")
            return
        self.show_rule_dialog(rule)

    def edit_selected_connection(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        connection = self._selected_connection()
        if connection is None:
            ui.notify("Select a connection first.", type="warning")
            return
        if self.current_role != Role.ADMIN and connection.owner_username != self.current_user:
            ui.notify("You can only edit connections that you own.", type="warning")
            return
        self.show_connection_dialog(connection)

    async def test_selected_connection(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        connection = self._selected_connection()
        if connection is None:
            ui.notify("Select a connection first.", type="warning")
            return
        ui.notify(f"Testing connection {connection.name}...", type="info")
        ok, message = await nicegui_run.io_bound(self.connector_service.test_connection, connection)
        ui.notify(message, type="positive" if ok else "negative")
        self._set_last_action(f"Tested connection {connection.name}: {'OK' if ok else 'failed'}")

    def delete_selected_connection(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        connection = self._selected_connection()
        if connection is None:
            ui.notify("Select a connection first.", type="warning")
            return
        if self.current_role != Role.ADMIN and connection.owner_username != self.current_user:
            ui.notify("You can only delete connections that you own.", type="warning")
            return
        referencing = [
            rule.name
            for rule in self.project.storage.list_rules()
            if connection.id in (rule.config.get("source_connection_id"), rule.config.get("target_connection_id"))
        ]
        message = f"Delete connection '{connection.name}'?"
        if referencing:
            message += f" It is used by {len(referencing)} rule(s): {', '.join(referencing[:5])}. Those rules will stop working."

        def do_delete() -> None:
            self.project.storage.delete_connection(int(connection.id or 0))
            self.refresh_all()
            self._set_last_action(f"Deleted connection {connection.name}")
            ui.notify(f"Deleted connection {connection.name}.", type="positive")

        self._confirm_delete(message, do_delete)

    def delete_selected_rule(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        rule = self._selected_rule()
        if rule is None:
            ui.notify("Select a rule first.", type="warning")
            return
        if self.current_role != Role.ADMIN and rule.owner_username != self.current_user:
            ui.notify("You can only delete rules that you own.", type="warning")
            return

        def do_delete() -> None:
            self.project.storage.delete_rule(int(rule.id or 0))
            self.refresh_all()
            self._set_last_action(f"Deleted rule {rule.name}")
            ui.notify(f"Deleted rule {rule.name}.", type="positive")

        self._confirm_delete(f"Delete rule '{rule.name}'? Its past results stay in the history.", do_delete)

    def show_group_dialog(self, group: RuleGroup | None = None) -> None:
        if not self.project:
            ui.notify("Open a project before creating a rule group.", type="warning")
            return
        rule_options = {str(item.id): f"{item.name} ({item.rule_type.value})" for item in self._visible_rules() if item.id is not None}
        if not rule_options:
            ui.notify("Create a rule before creating a rule group.", type="warning")
            return
        all_groups = self._visible_groups()
        groups_by_id = {item.id: item for item in all_groups if item.id is not None}
        blocked_ids = {group.id} | ancestor_group_ids(group.id, groups_by_id) if group and group.id is not None else set()
        parent_names_by_group_id = self._group_parent_names(all_groups)
        subgroup_options: dict[str, str] = {}
        for item in all_groups:
            if item.id is None or item.id in blocked_ids:
                continue
            used_in = parent_names_by_group_id.get(item.id, [])
            used_in_suffix = f", already used in: {', '.join(used_in)}" if used_in else ""
            subgroup_options[str(item.id)] = (
                f"{item.name} ({len(item.rule_ids)} rules, {len(item.child_group_ids)} subgroups{used_in_suffix})"
            )
        with ui.dialog() as dialog, ui.card().classes("dq-scroll-dialog w-[640px] max-w-full"):
            ui.label("BATCHES").classes("dq-eyebrow")
            ui.label("Edit rule group" if group else "Create a rule group").classes("dq-panel-title text-2xl font-bold")
            name = ui.input("Name", value=group.name if group else "").classes("w-full")
            visibility = ui.select(
                {"private": "private", "shared": "shared", "shared_specific": "shared_specific"},
                value=group.visibility if group else "private",
                label="Visibility",
            ).classes("w-full")
            allowed_users = ui.input(
                "Allowed Users (comma separated)",
                value=", ".join(group.allowed_users) if group else "",
            ).classes("w-full")
            rules_select = ui.select(
                rule_options,
                value=[str(rule_id) for rule_id in (group.rule_ids if group else []) if str(rule_id) in rule_options],
                label="Rules in this group",
                multiple=True,
            ).props("outlined use-chips options-dense").classes("w-full")
            if subgroup_options:
                subgroups_select = ui.select(
                    subgroup_options,
                    value=[
                        str(child_id)
                        for child_id in (group.child_group_ids if group else [])
                        if str(child_id) in subgroup_options
                    ],
                    label="Subgroups in this group",
                    multiple=True,
                ).props("outlined use-chips options-dense").classes("w-full")
                ui.label("A subgroup's own rules and subgroups run along with this group's.").classes(
                    "dq-panel-copy text-xs"
                )
            else:
                subgroups_select = None

            def sync_visibility() -> None:
                allowed_users.visible = visibility.value == "shared_specific"
                allowed_users.update()

            visibility.on_value_change(lambda _event: sync_visibility())
            sync_visibility()

            def save() -> None:
                try:
                    if not (name.value or "").strip():
                        raise ValueError("Group name is required.")
                    selected_rule_ids = [int(value) for value in (rules_select.value or [])]
                    selected_child_group_ids = [int(value) for value in (subgroups_select.value or [])] if subgroups_select else []
                    if not selected_rule_ids and not selected_child_group_ids:
                        raise ValueError("Select at least one rule or subgroup for the group.")
                    new_group = RuleGroup(
                        id=group.id if group else None,
                        name=(name.value or "").strip(),
                        owner_username=group.owner_username if group else self.current_user,
                        visibility=str(visibility.value),
                        allowed_users=self._split_csv_text(allowed_users.value),
                        rule_ids=selected_rule_ids,
                        child_group_ids=selected_child_group_ids,
                    )
                    group_id = self.project.storage.save_rule_group(new_group)
                    dialog.close()
                    self.refresh_all()
                    self._set_last_action(f"Saved rule group {new_group.name}")
                    ui.notify(f"Saved rule group #{group_id}: {new_group.name}", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("color=primary")
        dialog.open()

    def show_schedule_dialog(self, existing: Schedule | None = None) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("dq-scroll-dialog w-[560px] max-w-full gap-1"):
            ui.label("EDIT SCHEDULE" if existing else "NEW SCHEDULE").classes("dq-eyebrow")
            ui.label("Edit schedule" if existing else "Create a schedule").classes("dq-panel-title text-2xl font-bold")
            name = ui.input("Name", value=existing.name if existing else "").classes("w-full")

            target_kind = ui.select(
                {ScheduleTargetKind.RULE.value: "Rule", ScheduleTargetKind.GROUP.value: "Rule group"},
                value=(existing.target_kind.value if existing else ScheduleTargetKind.RULE.value),
                label="Target type",
            ).classes("w-full")
            target_select = ui.select(options={}, label="Target").props("outlined dense").classes("w-full")

            def refresh_target_options() -> None:
                if target_kind.value == ScheduleTargetKind.GROUP.value:
                    options = {str(item.id): item.name for item in self._visible_groups() if item.id is not None}
                else:
                    options = {str(item.id): item.name for item in self._visible_rules() if item.id is not None}
                preserve = (
                    str(existing.target_id)
                    if existing and existing.target_kind.value == target_kind.value
                    else target_select.value
                )
                target_select.options = options
                target_select.value = preserve if preserve in options else (next(iter(options), None))
                target_select.update()

            target_kind.on_value_change(lambda _event: refresh_target_options())
            refresh_target_options()

            cadence = ui.select(
                {item.value: item.value.capitalize() for item in ScheduleCadence},
                value=(existing.cadence.value if existing else ScheduleCadence.DAILY.value),
                label="Cadence",
            ).classes("w-full")
            interval_hours = ui.number(
                "Every N hours", value=(existing.interval_hours if existing else 1), min=1, format="%.0f"
            ).classes("w-full")
            time_of_day = ui.input(
                "Time (HH:MM, UTC)", value=(existing.time_of_day if existing else "09:00"), placeholder="09:00"
            ).classes("w-full")
            weekday = ui.select(
                dict(enumerate(WEEKDAY_NAMES)),
                value=(existing.weekday if existing else 0),
                label="Day of week",
            ).classes("w-full")
            enabled = ui.switch("Enabled", value=(existing.enabled if existing else True))

            def sync_cadence_visibility() -> None:
                selected = str(cadence.value)
                interval_hours.visible = selected == ScheduleCadence.HOURLY.value
                time_of_day.visible = selected in {ScheduleCadence.DAILY.value, ScheduleCadence.WEEKLY.value}
                weekday.visible = selected == ScheduleCadence.WEEKLY.value
                for element in (interval_hours, time_of_day, weekday):
                    element.update()

            cadence.on_value_change(lambda _event: sync_cadence_visibility())
            sync_cadence_visibility()

            def save() -> None:
                try:
                    if not target_select.value:
                        raise ValueError("Choose a target rule or group.")
                    selected_cadence = ScheduleCadence(cadence.value)
                    hour, minute = 0, 0
                    if selected_cadence in {ScheduleCadence.DAILY, ScheduleCadence.WEEKLY}:
                        time_text = str(time_of_day.value or "").strip()
                        try:
                            hour_text, minute_text = time_text.split(":", 1)
                            hour, minute = int(hour_text), int(minute_text)
                            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                                raise ValueError
                        except ValueError:
                            raise ValueError("Enter a valid time as HH:MM (24-hour, UTC).") from None
                    schedule = Schedule(
                        id=existing.id if existing else None,
                        name=(name.value or "").strip(),
                        target_kind=ScheduleTargetKind(target_kind.value),
                        target_id=int(target_select.value),
                        cadence=selected_cadence,
                        interval_hours=int(interval_hours.value or 1),
                        time_of_day=f"{hour:02d}:{minute:02d}",
                        weekday=int(weekday.value or 0),
                        enabled=bool(enabled.value),
                        owner_username=existing.owner_username if existing else self.current_user,
                        last_run_at=existing.last_run_at if existing else None,
                        last_status=existing.last_status if existing else None,
                    )
                    if not schedule.name:
                        raise ValueError("Schedule name is required.")
                    schedule.next_run_at = compute_next_run(schedule, after=datetime.now(UTC)).isoformat()
                    self.project.storage.save_schedule(schedule)
                    dialog.close()
                    self.refresh_all()
                    self._set_last_action(f"Saved schedule {schedule.name}")
                    ui.notify(f"Saved schedule {schedule.name}.", type="positive")
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("justify-end gap-2 w-full mt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save).props("color=primary unelevated")
        dialog.open()

    def _selected_schedule(self) -> Schedule | None:
        if not self.project:
            return None
        selected_id = self.schedule_select.value
        if not selected_id:
            return None
        schedule_id = int(selected_id)
        return next((item for item in self.project.storage.list_schedules() if item.id == schedule_id), None)

    def edit_selected_schedule(self) -> None:
        schedule = self._selected_schedule()
        if schedule is None:
            ui.notify("Select a schedule first.", type="warning")
            return
        self.show_schedule_dialog(schedule)

    def toggle_selected_schedule(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        schedule = self._selected_schedule()
        if schedule is None:
            ui.notify("Select a schedule first.", type="warning")
            return
        schedule.enabled = not schedule.enabled
        if schedule.enabled:
            schedule.next_run_at = compute_next_run(schedule, after=datetime.now(UTC)).isoformat()
        self.project.storage.save_schedule(schedule)
        self.refresh_all()
        state = "enabled" if schedule.enabled else "disabled"
        self._set_last_action(f"{state.capitalize()} schedule {schedule.name}")
        ui.notify(f"Schedule '{schedule.name}' {state}.", type="positive")

    async def run_selected_schedule_now(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        schedule = self._selected_schedule()
        if schedule is None:
            ui.notify("Select a schedule first.", type="warning")
            return
        rules_by_id = {item.id: item for item in self._visible_rules() if item.id is not None}
        groups_by_id = {item.id: item for item in self._visible_groups() if item.id is not None}
        if schedule.target_kind == ScheduleTargetKind.RULE:
            rule = rules_by_id.get(schedule.target_id)
            rules = [rule] if rule is not None else []
        else:
            group = groups_by_id.get(schedule.target_id)
            rules = resolve_group_rules(group, groups_by_id, rules_by_id)[0] if group is not None else []
        if not rules:
            ui.notify("The scheduled rule or group no longer exists or is not accessible.", type="warning")
            return
        try:
            runs, passed, failed, errored = await self._run_rules_batch(rules)
            self.refresh_all()
            self._set_last_action(f"Ran schedule {schedule.name} on demand")
            ui.notify(
                f"Schedule '{schedule.name}': {passed} passed, {failed} failed, {errored} errored ({len(runs)} rule(s)). "
                "This on-demand run did not change the schedule's next automatic run time.",
                type="positive" if failed == 0 and errored == 0 else "warning",
            )
        except Exception as exc:
            ui.notify(str(exc), type="negative")

    def delete_selected_schedule(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        schedule = self._selected_schedule()
        if schedule is None:
            ui.notify("Select a schedule first.", type="warning")
            return

        def do_delete() -> None:
            self.project.storage.delete_schedule(int(schedule.id or 0))
            self.refresh_all()
            self._set_last_action(f"Deleted schedule {schedule.name}")
            ui.notify(f"Deleted schedule {schedule.name}.", type="positive")

        self._confirm_delete(f"Delete schedule '{schedule.name}'? This does not delete the rule or group itself.", do_delete)

    def _on_schedule_select_change(self, event: Any) -> None:
        self._highlight_table_row(self.schedules_table, event.value)

    def _select_schedule_row(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None:
            return
        selected_id = str(row["id"])
        self.schedule_select.value = selected_id
        self.schedule_select.update()
        self._highlight_table_row(self.schedules_table, selected_id)

    def _populate_schedules(self) -> None:
        if not self.project:
            self.schedules_table.rows = []
            self.schedules_table.update()
            self._set_select_options(self.schedule_select, {}, None)
            return
        schedules = self.project.storage.list_schedules()
        rules_by_id = {item.id: item for item in self._visible_rules() if item.id is not None}
        groups_by_id = {item.id: item for item in self._visible_groups() if item.id is not None}
        rows: list[dict[str, Any]] = []
        options: dict[str, str] = {}
        for schedule in sorted(schedules, key=lambda item: item.name.lower()):
            if schedule.target_kind == ScheduleTargetKind.RULE:
                target = rules_by_id.get(schedule.target_id)
                target_label = f"Rule: {target.name}" if target else "Rule: (deleted)"
            else:
                target = groups_by_id.get(schedule.target_id)
                target_label = f"Group: {target.name}" if target else "Group: (deleted)"
            rows.append(
                {
                    "key": str(schedule.id),
                    "id": schedule.id,
                    "name": schedule.name,
                    "target": target_label,
                    "cadence": describe_cadence(schedule),
                    "enabled": "Yes" if schedule.enabled else "No",
                    "next_run": schedule.next_run_at or "-",
                    "last_run": schedule.last_run_at or "-",
                    "last_status": (schedule.last_status or "-").upper(),
                }
            )
            options[str(schedule.id)] = f"{schedule.name} ({target_label})"
        self.schedules_table.rows = rows
        self.schedules_table.update()
        self._set_select_options(self.schedule_select, options, self.schedule_select.value)

    def edit_selected_group(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        group = self._selected_group()
        if group is None:
            ui.notify("Select a rule group first.", type="warning")
            return
        if self.current_role != Role.ADMIN and group.owner_username != self.current_user:
            ui.notify("You can only edit rule groups that you own.", type="warning")
            return
        self.show_group_dialog(group)

    async def run_selected_group(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        group = self._selected_group()
        if group is None:
            ui.notify("Select a rule group first.", type="warning")
            return
        rules_by_id = {rule.id: rule for rule in self._visible_rules() if rule.id is not None}
        groups_by_id = {item.id: item for item in self._visible_groups() if item.id is not None}
        rules, missing_rules, missing_groups = resolve_group_rules(group, groups_by_id, rules_by_id)
        if not rules:
            ui.notify("The group has no rules you can access, directly or through subgroups.", type="warning")
            return
        self._confirm_run_group(group, rules, missing_rules, missing_groups)

    def _confirm_run_group(
        self,
        group: RuleGroup,
        rules: list[Rule],
        missing_rules: int,
        missing_groups: int,
    ) -> None:
        preview_names = [rule.name for rule in rules[:8]]
        preview = ", ".join(preview_names)
        if len(rules) > 8:
            preview += f", and {len(rules) - 8} more"
        message = f"Run {len(rules)} rule(s) in '{group.name}'? {preview}."
        skipped_bits = []
        if missing_rules:
            skipped_bits.append(f"{missing_rules} deleted or inaccessible rule(s)")
        if missing_groups:
            skipped_bits.append(f"{missing_groups} deleted or inaccessible subgroup(s)")
        if skipped_bits:
            message += " Will skip " + " and ".join(skipped_bits) + "."

        with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-full"):
            ui.label("Run rule group").classes("text-xl font-semibold")
            ui.label(message).classes("text-sm")

            async def confirm() -> None:
                dialog.close()
                await self._execute_group_rules(group, rules, missing_rules, missing_groups)

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Run", icon="playlist_play", on_click=confirm).props("color=secondary unelevated")
        dialog.open()

    async def _run_rules_batch(self, rules: list[Rule]) -> tuple[list[RuleRun], int, int, int]:
        """Run rules against the open project, persist the runs, and count the outcomes.

        Shared by the single-rule Run button, group runs, checked-rule batches, and the
        schedule "Run now" button. Returns (runs, passed, failed, errored).
        """
        connections = {connection.id: connection for connection in self._visible_connections() if connection.id is not None}
        runs = await nicegui_run.io_bound(
            self.execution_service.run_rules,
            rules,
            {},
            connections,
            self.project.results_dir,
            self.current_user,
        )
        for run in runs:
            self.project.storage.save_rule_run(run)
        passed = sum(1 for run in runs if run.status == "passed")
        failed = sum(1 for run in runs if run.status == "failed")
        errored = sum(1 for run in runs if run.status == "error")
        return runs, passed, failed, errored

    async def _execute_group_rules(
        self,
        group: RuleGroup,
        rules: list[Rule],
        missing_rules: int,
        missing_groups: int,
    ) -> None:
        try:
            runs, passed, failed, errored = await self._run_rules_batch(rules)
            self.refresh_all()
            self._set_last_action(f"Ran rule group {group.name}")
            message = f"Group '{group.name}': {passed} passed, {failed} failed, {errored} errored ({len(runs)} rules, incl. subgroups)."
            skipped_bits = []
            if missing_rules:
                skipped_bits.append(f"{missing_rules} deleted or inaccessible rule(s)")
            if missing_groups:
                skipped_bits.append(f"{missing_groups} deleted or inaccessible subgroup(s)")
            if skipped_bits:
                message += " Skipped " + " and ".join(skipped_bits) + "."
            ui.notify(message, type="positive" if failed == 0 and errored == 0 else "warning")
        except Exception as exc:
            ui.notify(str(exc), type="negative")

    def delete_selected_group(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        group = self._selected_group()
        if group is None:
            ui.notify("Select a rule group first.", type="warning")
            return
        if self.current_role != Role.ADMIN and group.owner_username != self.current_user:
            ui.notify("You can only delete rule groups that you own.", type="warning")
            return

        def do_delete() -> None:
            self.project.storage.delete_rule_group(int(group.id or 0))
            self.refresh_all()
            self._set_last_action(f"Deleted rule group {group.name}")
            ui.notify(f"Deleted rule group {group.name}.", type="positive")

        self._confirm_delete(f"Delete rule group '{group.name}'? The rules themselves are kept.", do_delete)

    def delete_selected_result(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        run = self._selected_result()
        if run is None:
            ui.notify("Select a result first.", type="warning")
            return
        if self.current_role != Role.ADMIN and run.executed_by != self.current_user:
            ui.notify("You can only delete results that you executed.", type="warning")
            return

        def do_delete() -> None:
            self.project.storage.delete_rule_run(int(run.id or 0))
            if run.failed_rows_path:
                # The failed-rows CSV is shared by all runs of the rule; only remove it with the last run.
                still_referenced = any(
                    other.failed_rows_path == run.failed_rows_path
                    for other in self.project.storage.list_rule_runs(limit=1000000)
                )
                if not still_referenced:
                    Path(run.failed_rows_path).unlink(missing_ok=True)
            self.refresh_all()
            self._set_last_action(f"Deleted run {run.id}")
            ui.notify(f"Deleted run {run.id}.", type="positive")

        self._confirm_delete(f"Delete run {run.id} and its failed-row data?", do_delete)

    def _confirm_delete(self, message: str, on_confirm: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
            ui.label("Confirm deletion").classes("text-xl font-semibold")
            ui.label(message).classes("text-sm")

            def confirm() -> None:
                dialog.close()
                try:
                    on_confirm()
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Delete", icon="delete", on_click=confirm).props("color=negative unelevated")
        dialog.open()

    async def run_selected_rule(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        rule = self._selected_rule()
        if rule is None:
            ui.notify("Select a rule first.", type="warning")
            return
        try:
            runs, _passed, _failed, _errored = await self._run_rules_batch([rule])
            self.refresh_all()
            self._set_last_action(f"Ran rule {rule.name}")
            self._notify_run_outcome(rule, runs[0])
        except Exception as exc:
            ui.notify(str(exc), type="negative")

    async def run_checked_rules(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        if not self._checked_rule_keys:
            ui.notify("Check at least one rule below first.", type="warning")
            return
        rules_by_id = {rule.id: rule for rule in self._visible_rules() if rule.id is not None}
        checked_ids = [int(key.split(":", 1)[1]) for key in self._checked_rule_keys]
        rules = [rules_by_id[rule_id] for rule_id in checked_ids if rule_id in rules_by_id]
        missing = len(checked_ids) - len(rules)
        if not rules:
            ui.notify("None of the checked rules are accessible anymore.", type="warning")
            return
        self._confirm_run_checked_rules(rules, missing)

    def _confirm_run_checked_rules(self, rules: list[Rule], missing: int) -> None:
        preview_names = [rule.name for rule in rules[:8]]
        preview = ", ".join(preview_names)
        if len(rules) > 8:
            preview += f", and {len(rules) - 8} more"
        message = f"Run {len(rules)} selected rule(s)? {preview}."
        if missing:
            message += f" Will skip {missing} deleted or inaccessible rule(s)."

        with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-full"):
            ui.label("Run selected rules").classes("text-xl font-semibold")
            ui.label(message).classes("text-sm")

            async def confirm() -> None:
                dialog.close()
                await self._execute_checked_rules(rules, missing)

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Run", icon="playlist_play", on_click=confirm).props("color=secondary unelevated")
        dialog.open()

    async def _execute_checked_rules(self, rules: list[Rule], missing: int) -> None:
        try:
            runs, passed, failed, errored = await self._run_rules_batch(rules)
            self._checked_rule_keys.clear()
            self.refresh_all()
            self._set_last_action(f"Ran {len(runs)} selected rule(s)")
            message = f"Selected rules: {passed} passed, {failed} failed, {errored} errored ({len(runs)} rule(s))."
            if missing:
                message += f" Skipped {missing} deleted or inaccessible rule(s)."
            ui.notify(message, type="positive" if failed == 0 and errored == 0 else "warning")
        except Exception as exc:
            ui.notify(str(exc), type="negative")

    def _notify_run_outcome(self, rule: Rule, run: RuleRun) -> None:
        summary = run.summary_json
        if run.status == "passed":
            failed_count = int(summary.get("failed_count") or 0)
            threshold_allowed = summary.get("fail_threshold_allowed")
            if failed_count and threshold_allowed:
                message = (
                    f"Rule '{rule.name}' passed with {failed_count:,} failed row(s), within its "
                    f"tolerance of {threshold_allowed:,} ({summary.get('checked_count', 0):,} rows checked)."
                )
            else:
                message = f"Rule '{rule.name}' passed ({summary.get('checked_count', 0):,} rows checked)."
            ui.notify(message, type="positive")
        elif run.status == "failed":
            ui.notify(
                f"Rule '{rule.name}' failed: {summary.get('failed_count', 0):,} of "
                f"{summary.get('checked_count', 0):,} rows.",
                type="warning",
            )
        else:
            ui.notify(f"Rule '{rule.name}' errored: {summary.get('error', 'Execution failed.')}", type="negative")

    def view_selected_result(self) -> None:
        run = self._selected_result()
        if run is None or not self.project:
            ui.notify("Select a result first.", type="warning")
            return
        rules = {rule.id: rule.name for rule in self.project.storage.list_rules()}
        summary = run.summary_json
        lines = [
            f"**Rule:** {rules.get(run.rule_id, f'Rule #{run.rule_id}')}",
            f"**Source:** {summary.get('source_label', f'Source for rule #{run.rule_id}')}",
            f"**Status:** {run.status.upper()}",
            f"**Checked rows:** {summary.get('checked_count', 'n/a')}",
            f"**Failed rows:** {summary.get('failed_count', 'n/a')}",
            f"**Started:** {self._format_timestamp(run.started_at)}",
            f"**Finished:** {self._format_timestamp(run.finished_at)}",
            f"**Executed by:** {run.executed_by}",
        ]
        if summary.get("error"):
            lines.append(f"**Error:** {summary['error']}")
        if run.failed_rows_path:
            lines.append(f"**Failed rows file:** `{run.failed_rows_path}`")
        self.result_details.content = "  \n".join(lines)
        self.result_details.update()
        self._set_last_action(f"Viewed run {run.id}")

    def preview_selected_failed_rows(self) -> None:
        run = self._selected_result()
        if run is None or not self.project:
            ui.notify("Select a result first.", type="warning")
            return
        rules = {rule.id: rule.name for rule in self.project.storage.list_rules()}
        rule_name = rules.get(run.rule_id, f"Rule #{run.rule_id}")
        source_name = run.summary_json.get("source_label", f"Source for rule #{run.rule_id}")
        if run.status == "error":
            self.failed_rows_label.text = f"{rule_name} on {source_name} | ERROR: {run.summary_json.get('error', 'Execution failed')}"
            self.failed_rows_label.update()
            return
        if not run.failed_rows_path:
            self.failed_rows_label.text = f"{rule_name} on {source_name} | This run has no failed rows to preview."
            self.failed_rows_label.update()
            return
        path = Path(run.failed_rows_path)
        if not path.exists():
            self.failed_rows_label.text = f"{rule_name} on {source_name} | Failed-rows file is missing: {path}"
            self.failed_rows_label.update()
            return

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            if "execution_datetime" in headers:
                data_rows = [row for row in reader if row.get("execution_datetime") == run.started_at]
            else:
                data_rows = list(reader)
        rows = data_rows[:200]

        table_columns = [{"name": header, "label": header, "field": header, "align": "left"} for header in headers]
        table_rows = [
            {"id": index, **{header: (row.get(header) or "") for header in headers}}
            for index, row in enumerate(rows)
        ]
        self.failed_rows_table.columns = table_columns
        self.failed_rows_table.rows = table_rows
        self.failed_rows_table.update()
        total_failed = run.summary_json.get("failed_count", len(rows))
        self.failed_rows_label.text = f"{rule_name} on {source_name} | Showing {len(rows):,} of {total_failed:,} failed rows for this run"
        self.failed_rows_label.update()
        self._set_last_action(f"Previewed failed rows for run {run.id}")

    async def preview_selected_connection_source(self) -> None:
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        connection = self._connection_from_select(self.preview_connection_select)
        if connection is None:
            ui.notify("Select a connection first.", type="warning")
            return
        target = str(self.preview_target_select.value or "").strip()
        if not target:
            ui.notify("Select a file or table first.", type="warning")
            return
        source_kind = "csv_file" if connection.connection_type == ConnectionType.CSV else "oracle_table"
        source_config = {
            "source_connection_id": int(connection.id or 0),
            "source_kind": source_kind,
            "source_name": target,
            "source_sql": "",
        }
        connections = {item.id: item for item in self._visible_connections() if item.id is not None}
        try:
            columns, rows, stats = await nicegui_run.io_bound(
                self.connector_service.preview_rule_source, source_config, connections
            )
        except Exception as exc:
            self.preview_log.content = f"Could not preview **{target}**: {exc}"
            self.preview_log.update()
            ui.notify(str(exc), type="negative")
            return
        preview_columns = [{"name": column, "label": column, "field": column, "align": "left"} for column in columns]
        preview_rows = []
        for index, row in enumerate(rows):
            item = {"id": index}
            for col_index, column in enumerate(columns):
                item[column] = row[col_index]
            preview_rows.append(item)
        self.preview_table.columns = preview_columns
        self.preview_table.rows = preview_rows
        self.preview_table.update()
        self.preview_log.content = (
            f"Previewed **{target}** on connection **{connection.name}**  \n```json\n{json.dumps(stats, indent=2)}\n```"
        )
        self.preview_log.update()
        self._set_last_action(f"Previewed {target} on {connection.name}")

    def _populate_dashboard(self) -> None:
        if not self.project:
            self.connections_count.text = "0"
            self.rules_count.text = "0"
            self.runs_count.text = "0"
            self.failures_count.text = "0"
            self.dashboard_markdown.content = "Open a project to begin."
            self.dashboard_markdown.update()
            self._set_chart_options(self.outcomes_chart, self._empty_chart_options("Open a project to see charts"))
            self._set_chart_options(self.top_failures_chart, self._empty_chart_options("Open a project to see charts"))
            return
        connections = self._visible_connections()
        rules = self._visible_rules()
        runs = self.project.storage.list_rule_runs(limit=20)
        self._update_dashboard_charts(
            self.project.storage.list_rule_runs(limit=100),
            {rule.id: rule.name for rule in self.project.storage.list_rules() if rule.id is not None},
        )
        failing = [run for run in runs if run.status == "failed"]
        self.connections_count.text = str(len(connections))
        self.rules_count.text = str(len(rules))
        self.runs_count.text = str(len(runs))
        self.failures_count.text = str(len(failing))
        lines = ["### Recent runs", ""]
        if not runs:
            lines.append("No runs yet. Create a rule, connect a source, and run your first quality check.")
        for run in runs[:8]:
            status_marker = "PASS" if run.status.lower() in {"passed", "success"} else run.status.upper()
            lines.append(
                f"**{status_marker}** &nbsp; Rule {run.rule_id} on "
                f"{run.summary_json.get('source_label', 'selected source')}  "
                f"\n{self._format_timestamp(run.started_at)} · {run.executed_by}\n"
            )
        self.dashboard_markdown.content = "\n".join(lines)
        self.dashboard_markdown.update()

    def _update_dashboard_charts(self, runs: list[RuleRun], rule_names: dict[int, str]) -> None:
        status_styles = RUN_STATUS_STYLES
        if not runs:
            self._set_chart_options(self.outcomes_chart, self._empty_chart_options("No runs yet"))
            self._set_chart_options(self.top_failures_chart, self._empty_chart_options("No runs yet"))
            return
        counts: dict[str, int] = {}
        latest_failed: dict[int, int] = {}
        for run in runs:  # runs arrive newest first, so the first run per rule is its latest
            counts[run.status] = counts.get(run.status, 0) + 1
            if run.rule_id not in latest_failed:
                latest_failed[run.rule_id] = int(run.summary_json.get("failed_count") or 0)
        self._set_chart_options(
            self.outcomes_chart,
            {
                "tooltip": {"trigger": "item"},
                "legend": {"bottom": 0, "textStyle": {"color": "#5c564d"}},
                "series": [
                    {
                        "type": "pie",
                        "radius": ["52%", "74%"],
                        "center": ["50%", "44%"],
                        "itemStyle": {"borderColor": "#ffffff", "borderWidth": 2, "borderRadius": 4},
                        "label": {"formatter": "{b}: {c}", "color": "#37332e"},
                        "data": [
                            {"value": counts[status], "name": label, "itemStyle": {"color": color}}
                            for status, (label, color) in status_styles.items()
                            if counts.get(status)
                        ],
                    }
                ],
            },
        )
        top_failing = sorted(
            (
                (rule_names.get(rule_id, f"Deleted rule #{rule_id}"), failed_count)
                for rule_id, failed_count in latest_failed.items()
                if failed_count > 0
            ),
            key=lambda item: item[1],
        )[-8:]
        if not top_failing:
            self._set_chart_options(self.top_failures_chart, self._empty_chart_options("No failed rows in the latest runs"))
            return
        self._set_chart_options(
            self.top_failures_chart,
            {
                "grid": {"left": 8, "right": 48, "top": 8, "bottom": 8, "containLabel": True},
                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                "xAxis": {
                    "type": "value",
                    "splitLine": {"lineStyle": {"color": "#e2ded7"}},
                    "axisLabel": {"color": "#837d74"},
                },
                "yAxis": {
                    "type": "category",
                    "data": [name for name, _ in top_failing],
                    "axisLabel": {"color": "#5c564d"},
                    "axisLine": {"lineStyle": {"color": "#e2ded7"}},
                },
                "series": [
                    {
                        "type": "bar",
                        "data": [failed_count for _, failed_count in top_failing],
                        "barMaxWidth": 18,
                        "itemStyle": {"color": "#6f6960", "borderRadius": [0, 4, 4, 0]},
                        "label": {"show": True, "position": "right", "color": "#37332e"},
                    }
                ],
            },
        )

    def _update_results_outcome_chart(self, runs: list[RuleRun]) -> None:
        if not runs:
            self._set_chart_options(self.results_outcome_chart, self._empty_chart_options("No runs yet"))
            return
        per_day: dict[str, dict[str, int]] = {}
        for run in runs:
            day = str(run.started_at)[:10]
            bucket = per_day.setdefault(day, {})
            bucket[run.status] = bucket.get(run.status, 0) + 1
        days = sorted(per_day)[-14:]
        self._set_chart_options(
            self.results_outcome_chart,
            {
                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                "legend": {"bottom": 0, "textStyle": {"color": "#5c564d"}},
                "grid": {"left": 8, "right": 16, "top": 12, "bottom": 32, "containLabel": True},
                "xAxis": {"type": "category", "data": days, "axisLabel": {"color": CHART_MUTED, "fontSize": 10}},
                "yAxis": {
                    "type": "value",
                    "minInterval": 1,
                    "splitLine": {"lineStyle": {"color": CHART_GRID}},
                    "axisLabel": {"color": CHART_MUTED},
                },
                "series": [
                    {
                        "name": label,
                        "type": "bar",
                        "stack": "runs",
                        "data": [per_day[day].get(status, 0) for day in days],
                        "itemStyle": {"color": color, "borderColor": "#ffffff", "borderWidth": 1},
                        "barMaxWidth": 26,
                    }
                    for status, (label, color) in RUN_STATUS_STYLES.items()
                ],
            },
        )

    def _update_result_trend_chart(self) -> None:
        rule_id = self._selected_result_rule_id()
        if rule_id is None:
            self._set_chart_options(self.results_trend_chart, self._empty_chart_options("Select a rule"))
            return
        history = sorted(
            (
                item
                for item in self.project.storage.list_rule_runs(limit=500)
                if item.rule_id == rule_id and item.status != "error"
            ),
            key=lambda item: item.started_at,
        )
        if not history:
            self._set_chart_options(
                self.results_trend_chart, self._empty_chart_options("No completed executions for this rule yet")
            )
            return
        rule_names = {rule.id: rule.name for rule in self.project.storage.list_rules() if rule.id is not None}
        rule_name = rule_names.get(rule_id, f"Deleted rule #{rule_id}")
        self._set_chart_options(
            self.results_trend_chart,
            {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 8, "right": 24, "top": 16, "bottom": 8, "containLabel": True},
                "xAxis": {
                    "type": "category",
                    "data": [self._format_timestamp(item.started_at) for item in history],
                    "axisLabel": {"color": CHART_MUTED, "rotate": 30, "fontSize": 10},
                },
                "yAxis": {
                    "type": "value",
                    "minInterval": 1,
                    "splitLine": {"lineStyle": {"color": CHART_GRID}},
                    "axisLabel": {"color": CHART_MUTED},
                },
                "series": [
                    {
                        "name": f"Failed rows · {rule_name}",
                        "type": "line",
                        "data": [int(item.summary_json.get("failed_count") or 0) for item in history],
                        "lineStyle": {"width": 2, "color": CHART_SERIES},
                        "itemStyle": {"color": CHART_SERIES},
                        "symbolSize": 8,
                    }
                ],
            },
        )

    def _update_anomaly_charts(self, profile: dict[str, Any], source_key: str) -> None:
        null_rates = sorted(
            (
                (name, round(float(stats.get("null_rate") or 0) * 100, 1))
                for name, stats in profile.get("columns", {}).items()
            ),
            key=lambda item: item[1],
        )
        null_rates = [item for item in null_rates if item[1] > 0][-10:]
        if not null_rates:
            self._set_chart_options(self.anomaly_nulls_chart, self._empty_chart_options("No null values in this snapshot"))
        else:
            self._set_chart_options(
                self.anomaly_nulls_chart,
                {
                    "grid": {"left": 8, "right": 56, "top": 8, "bottom": 8, "containLabel": True},
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "xAxis": {
                        "type": "value",
                        "axisLabel": {"color": CHART_MUTED, "formatter": "{value}%"},
                        "splitLine": {"lineStyle": {"color": CHART_GRID}},
                    },
                    "yAxis": {
                        "type": "category",
                        "data": [name for name, _ in null_rates],
                        "axisLabel": {"color": "#5c564d"},
                        "axisLine": {"lineStyle": {"color": CHART_GRID}},
                    },
                    "series": [
                        {
                            "type": "bar",
                            "data": [rate for _, rate in null_rates],
                            "barMaxWidth": 18,
                            "itemStyle": {"color": CHART_SERIES, "borderRadius": [0, 4, 4, 0]},
                            "label": {"show": True, "position": "right", "color": CHART_INK, "formatter": "{c}%"},
                        }
                    ],
                },
            )
        history = self.project.storage.list_source_profiles(source_key) if self.project else []
        if not history:
            self._set_chart_options(self.anomaly_rowcount_chart, self._empty_chart_options("No snapshots yet"))
            return
        self._set_chart_options(
            self.anomaly_rowcount_chart,
            {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 8, "right": 24, "top": 16, "bottom": 8, "containLabel": True},
                "xAxis": {
                    "type": "category",
                    "data": [self._format_timestamp(item.get("profiled_at")) for item in history],
                    "axisLabel": {"color": CHART_MUTED, "rotate": 30, "fontSize": 10},
                },
                "yAxis": {
                    "type": "value",
                    "minInterval": 1,
                    "splitLine": {"lineStyle": {"color": CHART_GRID}},
                    "axisLabel": {"color": CHART_MUTED},
                },
                "series": [
                    {
                        "name": "Rows",
                        "type": "line",
                        "data": [int(item.get("row_count") or 0) for item in history],
                        "lineStyle": {"width": 2, "color": CHART_SERIES},
                        "itemStyle": {"color": CHART_SERIES},
                        "symbolSize": 8,
                    }
                ],
            },
        )

    def _empty_chart_options(self, message: str) -> dict[str, Any]:
        return {
            "graphic": [
                {
                    "type": "text",
                    "left": "center",
                    "top": "middle",
                    "style": {"text": message, "fill": "#837d74", "fontSize": 14},
                }
            ],
            "series": [],
        }

    def _set_chart_options(self, chart: ui.echart, options: dict[str, Any]) -> None:
        chart.options.clear()
        chart.options.update(options)
        chart.update()

    def _populate_connections(self) -> None:
        rows = []
        for item in self._visible_connections():
            rows.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "type": item.connection_type.value,
                    "owner": item.owner_username,
                    "visibility": item.visibility,
                }
            )
        self.connections_table.rows = rows
        self.connections_table.update()
        options = {
            str(item.id): f"{item.name} · {item.connection_type.value.upper()}"
            for item in self._visible_connections()
            if item.id is not None
        }
        self._set_select_options(self.connection_select, options, self.selected_connection_id)
        self._set_select_options(
            self.anomaly_connection_select, options, self._id_to_str(self.anomaly_connection_select.value)
        )
        self._set_select_options(
            self.preview_connection_select, options, self._id_to_str(self.preview_connection_select.value)
        )
        self._highlight_table_row(self.connections_table, self.connection_select.value)

    def _populate_rules_and_groups(self) -> None:
        """Build one tree: each root group, its subgroups and member rules indented below it,
        then any rule that isn't in a group. Rules used by more than one group appear once per
        group (the "Used In" column always lists every group that references them)."""
        rules = self._visible_rules()
        rules_by_id = {rule.id: rule for rule in rules if rule.id is not None}
        groups = self._visible_groups()
        groups_by_id = {item.id: item for item in groups if item.id is not None}
        group_parent_names = self._group_parent_names(groups)
        rule_parent_names: dict[int, list[str]] = {}
        for group in groups:
            for rule_id in group.rule_ids:
                rule_parent_names.setdefault(rule_id, []).append(group.name)
        root_group_ids = [item.id for item in groups if item.id is not None and item.id not in group_parent_names]

        rows: list[dict[str, Any]] = []
        referenced_rule_ids: set[int] = set()
        visited_group_ids: set[int] = set()
        counter = 0

        def add_group_row(group: RuleGroup, depth: int, parent_key: str | None) -> str:
            nonlocal counter
            total_rules, _missing_rules, _missing_groups = resolve_group_rules(group, groups_by_id, rules_by_id)
            direct_count = len(group.rule_ids)
            total_count = len(total_rules)
            rules_summary = f"{total_count} total" if total_count == direct_count else f"{direct_count} direct / {total_count} total"
            subgroup_count = len(group.child_group_ids)
            details = f"{rules_summary} rule(s)" + (f", {subgroup_count} subgroup(s)" if subgroup_count else "")
            used_in = group_parent_names.get(group.id, [])
            counter += 1
            stable_key = f"group:{group.id}"
            rows.append(
                {
                    "key": f"group-{group.id}-{counter}",
                    "stable_key": stable_key,
                    "parent_key": parent_key,
                    "id": group.id,
                    "kind": "group",
                    "depth": depth,
                    "name": group.name,
                    "details": details,
                    "owner": group.owner_username,
                    "visibility": group.visibility,
                    "used_in": ", ".join(used_in) if used_in else "-",
                }
            )
            return stable_key

        def add_rule_row(rule: Rule, depth: int, parent_key: str | None) -> None:
            nonlocal counter
            referenced_rule_ids.add(rule.id)
            used_in = rule_parent_names.get(rule.id, [])
            counter += 1
            rows.append(
                {
                    "key": f"rule-{rule.id}-{counter}",
                    "stable_key": f"rule:{rule.id}",
                    "parent_key": parent_key,
                    "id": rule.id,
                    "kind": "rule",
                    "depth": depth,
                    "name": rule.name,
                    "details": rule.rule_type.value + (f" — {rule.description}" if rule.description else ""),
                    "owner": rule.owner_username,
                    "visibility": rule.visibility,
                    "used_in": ", ".join(used_in) if used_in else "-",
                    "checked": f"rule:{rule.id}" in self._checked_rule_keys,
                }
            )

        def visit_group(group_id: int, depth: int, parent_key: str | None) -> None:
            if group_id in visited_group_ids or group_id not in groups_by_id:
                return
            visited_group_ids.add(group_id)
            group = groups_by_id[group_id]
            own_key = add_group_row(group, depth, parent_key)
            for rule_id in group.rule_ids:
                rule = rules_by_id.get(rule_id)
                if rule is not None:
                    add_rule_row(rule, depth + 1, own_key)
            for child_id in group.child_group_ids:
                visit_group(child_id, depth + 1, own_key)

        for root_id in root_group_ids:
            visit_group(root_id, 0, None)
        # Defensive fallback: a group somehow unreachable from any root (should not happen once
        # every group has been through save-time cycle validation) still needs a row.
        for group in groups:
            if group.id is not None and group.id not in visited_group_ids:
                visit_group(group.id, 0, None)

        for rule in rules:
            if rule.id is not None and rule.id not in referenced_rule_ids:
                add_rule_row(rule, 0, None)

        child_counts: dict[str, int] = {}
        for row in rows:
            if row.get("parent_key"):
                child_counts[row["parent_key"]] = child_counts.get(row["parent_key"], 0) + 1
        for row in rows:
            row["has_children"] = child_counts.get(row["stable_key"], 0) > 0

        options: dict[str, str] = {}
        for row in rows:
            if row["stable_key"] not in options:
                prefix = "\U0001f4c1 " if row["kind"] == "group" else ""
                options[row["stable_key"]] = f"{prefix}{row['name']} ({row['details']})"

        # Drop checkmarks for rules that were deleted or became inaccessible since last checked.
        existing_rule_keys = {row["stable_key"] for row in rows if row["kind"] == "rule"}
        self._checked_rule_keys &= existing_rule_keys

        self._overview_all_rows = rows
        self._set_item_select_options(options)
        self._refresh_overview_view()

    def _group_parent_names(self, groups: list[RuleGroup]) -> dict[int, list[str]]:
        """Reverse lookup: for each group id, which other visible groups nest it as a subgroup."""
        parents: dict[int, list[str]] = {}
        for candidate in groups:
            for child_id in candidate.child_group_ids:
                parents.setdefault(child_id, []).append(candidate.name)
        return parents

    def _populate_results(self) -> None:
        if not self.project:
            self._results_all_rows = []
            self.rule_summary_table.rows = []
            self.rule_summary_table.update()
            self.results_table.rows = []
            self.results_table.update()
            self._set_chart_options(self.results_outcome_chart, self._empty_chart_options("Open a project to see charts"))
            self._set_chart_options(self.results_trend_chart, self._empty_chart_options("Open a project to see charts"))
            return
        runs = self.project.storage.list_rule_runs()
        rules = self._visible_rules()
        rules_by_id = {rule.id: rule for rule in rules if rule.id is not None}
        groups = self._visible_groups()
        groups_by_id = {item.id: item for item in groups if item.id is not None}
        group_parent_names = self._group_parent_names(groups)
        root_group_ids = [item.id for item in groups if item.id is not None and item.id not in group_parent_names]

        runs_by_rule_id: dict[int, list[RuleRun]] = {}
        for run in runs:  # newest first, so each rule's first entry is its latest run
            runs_by_rule_id.setdefault(run.rule_id, []).append(run)
        status_severity = {"passed": 0, "failed": 1, "error": 2}

        def aggregate_stats(rule_ids: list[int]) -> dict[str, Any]:
            latest_by_rule = {rule_id: runs_by_rule_id[rule_id][0] for rule_id in rule_ids if runs_by_rule_id.get(rule_id)}
            total_runs = sum(len(runs_by_rule_id.get(rule_id, [])) for rule_id in rule_ids)
            if not latest_by_rule:
                return {"runs": total_runs, "last_status": "-", "last_run": "-", "last_failed": "-"}
            worst = max(latest_by_rule.values(), key=lambda run: status_severity.get(run.status, 0))
            newest = max(latest_by_rule.values(), key=lambda run: run.started_at)
            total_failed = sum(int(run.summary_json.get("failed_count") or 0) for run in latest_by_rule.values())
            return {
                "runs": total_runs,
                "last_status": worst.status.upper(),
                "last_run": self._format_timestamp(newest.started_at),
                "last_failed": total_failed,
            }

        rows: list[dict[str, Any]] = []
        referenced_rule_ids: set[int] = set()
        visited_group_ids: set[int] = set()
        counter = 0

        def add_group_row(group: RuleGroup, depth: int, parent_key: str | None) -> str:
            nonlocal counter
            resolved_rules, _missing_rules, _missing_groups = resolve_group_rules(group, groups_by_id, rules_by_id)
            stats = aggregate_stats([rule.id for rule in resolved_rules if rule.id is not None])
            subgroup_count = len(group.child_group_ids)
            details = f"{len(resolved_rules)} rule(s)" + (f", {subgroup_count} subgroup(s)" if subgroup_count else "")
            counter += 1
            stable_key = f"group:{group.id}"
            rows.append(
                {
                    "key": f"group-{group.id}-{counter}",
                    "stable_key": stable_key,
                    "parent_key": parent_key,
                    "id": group.id,
                    "kind": "group",
                    "depth": depth,
                    "name": group.name,
                    "details": details,
                    **stats,
                }
            )
            return stable_key

        def add_rule_row(rule: Rule, depth: int, parent_key: str | None) -> None:
            nonlocal counter
            referenced_rule_ids.add(rule.id)
            stats = aggregate_stats([rule.id])
            rule_runs = runs_by_rule_id.get(rule.id, [])
            details = rule_runs[0].summary_json.get("source_label", "-") if rule_runs else "-"
            counter += 1
            rows.append(
                {
                    "key": f"rule-{rule.id}-{counter}",
                    "stable_key": f"rule:{rule.id}",
                    "parent_key": parent_key,
                    "id": rule.id,
                    "kind": "rule",
                    "depth": depth,
                    "name": rule.name,
                    "details": details,
                    **stats,
                }
            )

        def visit_group(group_id: int, depth: int, parent_key: str | None) -> None:
            if group_id in visited_group_ids or group_id not in groups_by_id:
                return
            visited_group_ids.add(group_id)
            group = groups_by_id[group_id]
            own_key = add_group_row(group, depth, parent_key)
            for rule_id in group.rule_ids:
                rule = rules_by_id.get(rule_id)
                if rule is not None:
                    add_rule_row(rule, depth + 1, own_key)
            for child_id in group.child_group_ids:
                visit_group(child_id, depth + 1, own_key)

        for root_id in root_group_ids:
            visit_group(root_id, 0, None)
        for group in groups:
            if group.id is not None and group.id not in visited_group_ids:
                visit_group(group.id, 0, None)
        for rule in rules:
            if rule.id is not None and rule.id not in referenced_rule_ids:
                add_rule_row(rule, 0, None)

        child_counts: dict[str, int] = {}
        for row in rows:
            if row.get("parent_key"):
                child_counts[row["parent_key"]] = child_counts.get(row["parent_key"], 0) + 1
        for row in rows:
            row["has_children"] = child_counts.get(row["stable_key"], 0) > 0

        options: dict[str, str] = {}
        for row in rows:
            if row["stable_key"] not in options:
                prefix = "\U0001f4c1 " if row["kind"] == "group" else ""
                suffix = f" ({row['runs']} run{'' if row['runs'] == 1 else 's'})" if row["runs"] else ""
                options[row["stable_key"]] = f"{prefix}{row['name']}{suffix}"

        self._results_all_rows = rows
        self._set_select_options(self.result_rule_select, options, self.selected_result_rule_id)
        self._refresh_results_view()
        self._update_results_outcome_chart(runs)

    def _selected_result_rule_id(self) -> int | None:
        selected = self.result_rule_select.value if self.project else None
        if not selected or not str(selected).startswith("rule:"):
            return None
        return int(str(selected).split(":", 1)[1])

    def _populate_result_runs(self) -> None:
        if not self.project:
            return
        rule_id = self._selected_result_rule_id()
        runs = [run for run in self.project.storage.list_rule_runs() if rule_id is not None and run.rule_id == rule_id]
        rows = []
        options: dict[str, str] = {}
        for run in runs:
            summary = run.summary_json
            rows.append(
                {
                    "id": run.id,
                    "run": run.id,
                    "status": run.status.upper(),
                    "checked": summary.get("checked_count", ""),
                    "failed": summary.get("failed_count", ""),
                    "started": self._format_timestamp(run.started_at),
                    "failed_rows_file": Path(run.failed_rows_path).name if run.failed_rows_path else "",
                }
            )
            options[str(run.id)] = f"Run {run.id} | {self._format_timestamp(run.started_at)} | {run.status.upper()}"
        self.results_table.rows = rows
        self.results_table.update()
        self._set_select_options(self.result_select, options, self.selected_run_id)
        self._highlight_table_row(self.results_table, self.result_select.value)

    def _populate_users(self) -> None:
        is_workspace_admin = self.signed_in and self.workspace_role == WorkspaceRole.WORKSPACE_ADMIN
        if not self.workspace or not is_workspace_admin:
            self.users_table.rows = []
            self.users_table.update()
            self._populate_project_members()
            return
        storage = self.workspace.storage
        memberships: dict[str, list[str]] = {}
        for project in storage.list_projects():
            for member, member_role in storage.list_project_members(project.id).items():
                memberships.setdefault(member, []).append(f"{project.name} ({member_role.value})")
        rows = [
            {
                "id": user.id,
                "username": user.username,
                "workspace_role": user.role.value,
                "projects": "All (Workspace Admin)"
                if user.role == WorkspaceRole.WORKSPACE_ADMIN
                else ", ".join(sorted(memberships.get(user.username, []))) or "-",
            }
            for user in storage.list_users()
        ]
        self.users_table.rows = rows
        self.users_table.update()

    def _administered_projects(self) -> list[Project]:
        """Projects where the signed-in user may manage members."""
        if not self.workspace or not self.signed_in:
            return []
        projects = self.workspace.storage.projects_for_user(self.current_user)
        if self.workspace_role == WorkspaceRole.WORKSPACE_ADMIN:
            return projects
        return [
            project
            for project in projects
            if project.id is not None
            and self.workspace.storage.role_in_project(self.current_user, project.id) == Role.ADMIN
        ]

    def _populate_project_members(self) -> None:
        projects = self._administered_projects()
        options = {str(project.id): project.name for project in projects}
        current = str(self.member_project_select.value) if self.member_project_select.value else None
        if current not in options:
            if self.current_project is not None and str(self.current_project.id) in options:
                current = str(self.current_project.id)
            else:
                current = next(iter(options), None)
        self.member_project_select.options = options
        self.member_project_select.value = current
        self.member_project_select.update()
        self._refresh_member_rows()

    def _refresh_member_rows(self) -> None:
        options = self.member_project_select.options or {}
        value = str(self.member_project_select.value) if self.member_project_select.value else None
        if not self.workspace or not self.signed_in or not value or value not in options:
            self.members_title.text = "Project members"
            self.members_table.rows = []
            self.members_table.update()
            self.member_user_select.options = []
            self.member_user_select.update()
            return
        storage = self.workspace.storage
        self.members_title.text = f"Members of {options[value]}"
        members = storage.list_project_members(int(value))
        self.members_table.rows = [
            {"id": index + 1, "username": username, "project_role": role.value}
            for index, (username, role) in enumerate(sorted(members.items()))
        ]
        self.members_table.update()
        self.member_user_select.options = [
            user.username for user in storage.list_users() if user.role != WorkspaceRole.WORKSPACE_ADMIN
        ]
        self.member_user_select.update()

    def _selected_member_project(self) -> tuple[int, str] | None:
        """The project selected in the members card, verified against the user's admin rights."""
        if not self.workspace or not self.signed_in:
            return None
        value = self.member_project_select.value
        if not value:
            return None
        project_id = int(value)
        if self.workspace.storage.role_in_project(self.current_user, project_id) != Role.ADMIN:
            return None
        name = (self.member_project_select.options or {}).get(str(value), "project")
        return project_id, name

    def save_project_member(self) -> None:
        selected = self._selected_member_project()
        if selected is None:
            ui.notify("Pick a project you administer first.", type="warning")
            return
        project_id, project_name = selected
        username = str(self.member_user_select.value or "").strip()
        if not username:
            ui.notify("Pick an account first.", type="warning")
            return
        target = self.workspace.storage.get_user(username)
        if target is None:
            ui.notify("That account does not exist. The Workspace Admin can create it with 'Add user'.", type="warning")
            return
        if target.role == WorkspaceRole.WORKSPACE_ADMIN:
            ui.notify("Workspace Admins already have access to every project.", type="info")
            return
        role = Role(self.member_role_select.value)
        self.workspace.storage.set_project_member(project_id, username, role)
        self._refresh_member_rows()
        self._populate_users()
        self._set_last_action(f"Set {username} as {role.value} in {project_name}")
        ui.notify(f"{username} is now {role.value} in {project_name}.", type="positive")

    def remove_project_member(self) -> None:
        selected = self._selected_member_project()
        if selected is None:
            ui.notify("Pick a project you administer first.", type="warning")
            return
        project_id, project_name = selected
        username = str(self.member_user_select.value or "").strip()
        if not username:
            ui.notify("Pick an account first.", type="warning")
            return
        self.workspace.storage.remove_project_member(project_id, username)
        self._refresh_member_rows()
        self._populate_users()
        self._set_last_action(f"Removed {username} from {project_name}")
        ui.notify(f"Removed {username} from {project_name}.", type="positive")

    def _select_member_row(self, event: Any) -> None:
        row = event.args[1] if len(event.args) > 1 else None
        if isinstance(row, dict) and row.get("username"):
            self.member_user_select.value = str(row["username"])
            self.member_role_select.value = row.get("project_role", Role.USER.value)
            self.member_user_select.update()
            self.member_role_select.update()
            self.members_table.selected = [row]
            self.members_table.update()

    def _select_user_row(self, event: Any) -> None:
        row = event.args[1] if len(event.args) > 1 else None
        if isinstance(row, dict) and row.get("username"):
            self._selected_username = str(row["username"])
            self.users_table.selected = [row]
            self.users_table.update()

    def edit_selected_user(self) -> None:
        username = getattr(self, "_selected_username", None)
        if not username:
            ui.notify("Select a user row first.", type="warning")
            return
        self.show_user_dialog(username)

    def _selected_connection(self) -> Connection | None:
        if not self.project:
            return None
        selected_id = self.connection_select.value
        self.selected_connection_id = str(selected_id) if selected_id else self.selected_connection_id
        if not selected_id:
            return None
        connection_id = int(selected_id)
        return next((item for item in self._visible_connections() if item.id == connection_id), None)

    def _selected_rule(self) -> Rule | None:
        if not self.project:
            return None
        value = self.item_select.value
        if not value or not str(value).startswith("rule:"):
            return None
        rule_id = int(str(value).split(":", 1)[1])
        return next((item for item in self.project.storage.list_rules() if item.id == rule_id), None)

    def _selected_group(self) -> RuleGroup | None:
        if not self.project:
            return None
        value = self.item_select.value
        if not value or not str(value).startswith("group:"):
            return None
        group_id = int(str(value).split(":", 1)[1])
        return next((item for item in self._visible_groups() if item.id == group_id), None)

    def edit_selected_item(self) -> None:
        if self._selected_group() is not None:
            self.edit_selected_group()
        elif self._selected_rule() is not None:
            self.edit_selected_rule()
        else:
            ui.notify("Select a rule or group first.", type="warning")

    def move_selected_rule_to_group(self) -> None:
        """Offer one-step direct-membership moves from the Rules overview."""
        if not self.project:
            ui.notify("Open a project first.", type="warning")
            return
        rule = self._selected_rule()
        if rule is None:
            ui.notify("Select the rule you want to move first.", type="warning")
            return
        if self.current_role != Role.ADMIN and rule.owner_username != self.current_user:
            ui.notify("You can only move rules that you own.", type="warning")
            return

        all_groups = self.project.storage.list_rule_groups()
        manageable_groups = [
            group for group in all_groups if self.current_role == Role.ADMIN or group.owner_username == self.current_user
        ]
        if not manageable_groups:
            ui.notify("Create a rule group that you own before moving this rule.", type="warning")
            return
        direct_memberships = [group for group in all_groups if rule.id in group.rule_ids]
        manageable_memberships = [group for group in direct_memberships if group in manageable_groups]
        retained_memberships = [group for group in direct_memberships if group not in manageable_groups]
        options = {
            str(group.id): f"{group.name}" + (" (current)" if group in direct_memberships else "")
            for group in manageable_groups
            if group.id is not None
        }

        with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
            ui.label("MOVE RULE").classes("dq-eyebrow")
            ui.label(f"Move '{rule.name}' to a group").classes("dq-panel-title text-xl font-bold")
            ui.label(
                "The rule will be added to the chosen group and removed from your other editable groups."
            ).classes("dq-panel-copy text-sm")
            if retained_memberships:
                ui.label(
                    "It will remain in groups managed by other users: " + ", ".join(group.name for group in retained_memberships) + "."
                ).classes("dq-panel-copy text-xs")
            target = ui.select(options, label="Destination group").props("outlined").classes("w-full")

            def move() -> None:
                if not target.value:
                    ui.notify("Choose a destination group.", type="warning")
                    return
                target_id = int(target.value)
                self.project.storage.move_rule_to_group(
                    int(rule.id or 0), target_id, [int(group.id or 0) for group in manageable_memberships]
                )
                destination = next(group.name for group in manageable_groups if group.id == target_id)
                dialog.close()
                self.refresh_all()
                self._set_last_action(f"Moved rule {rule.name} to {destination}")
                ui.notify(f"Moved '{rule.name}' to {destination}.", type="positive")

            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Move", icon="drive_file_move", on_click=move).props("color=primary")
        dialog.open()

    async def run_selected_item(self) -> None:
        if self._selected_group() is not None:
            await self.run_selected_group()
        elif self._selected_rule() is not None:
            await self.run_selected_rule()
        else:
            ui.notify("Select a rule or group first.", type="warning")

    def delete_selected_item(self) -> None:
        if self._selected_group() is not None:
            self.delete_selected_group()
        elif self._selected_rule() is not None:
            self.delete_selected_rule()
        else:
            ui.notify("Select a rule or group first.", type="warning")

    def _selected_result(self) -> RuleRun | None:
        if not self.project:
            return None
        selected_id = self.result_select.value
        self.selected_run_id = str(selected_id) if selected_id else self.selected_run_id
        if not selected_id:
            return None
        run_id = int(selected_id)
        return next((item for item in self.project.storage.list_rule_runs() if item.id == run_id), None)

    def _visible_connections(self) -> list[Connection]:
        if not self.project:
            return []
        return [item for item in self.project.storage.list_connections() if self._can_view(item.owner_username, item.visibility, item.allowed_users)]

    def _visible_rules(self) -> list[Rule]:
        if not self.project:
            return []
        return [item for item in self.project.storage.list_rules() if self._can_view(item.owner_username, item.visibility, item.allowed_users)]

    def _visible_groups(self) -> list[RuleGroup]:
        if not self.project:
            return []
        return [item for item in self.project.storage.list_rule_groups() if self._can_view(item.owner_username, item.visibility, item.allowed_users)]

    def _can_view(self, owner_username: str, visibility: str, allowed_users: list[str]) -> bool:
        if self.current_role == Role.ADMIN or owner_username == self.current_user:
            return True
        if visibility == "shared":
            return True
        return self.current_user in allowed_users

    def _load_rule_targets(self, select: ui.select, output: ui.markdown) -> None:
        try:
            connection_id = select.value
            if not connection_id:
                raise ValueError("Select a connection first.")
            connection = next(item for item in self._visible_connections() if str(item.id) == str(connection_id))
            targets = self.connector_service.list_connection_targets(connection)
            output.content = "\n".join(f"- {target}" for target in targets[:100]) or "_No targets found._"
            output.update()
        except Exception as exc:
            output.content = f"Could not load targets: {exc}"
            output.update()

    def _load_rule_columns(
        self,
        connection_select: ui.select,
        kind_select: ui.select,
        name_input: ui.input,
        sql_input: ui.textarea,
        output: ui.markdown,
    ) -> None:
        try:
            if not self.project:
                raise ValueError("Open a project first.")
            connection_id = connection_select.value
            if not connection_id:
                raise ValueError("Select a connection first.")
            config = {
                "source_connection_id": int(connection_id),
                "source_kind": kind_select.value,
                "source_name": (name_input.value or "").strip(),
                "source_sql": (sql_input.value or "").strip(),
            }
            connections = {item.id: item for item in self._visible_connections() if item.id is not None}
            columns = self.connector_service.list_rule_source_columns(config, connections)
            output.content = "\n".join(f"- {column}" for column in columns) or "_No columns found._"
            output.update()
        except Exception as exc:
            output.content = f"Could not load columns: {exc}"
            output.update()

    def _open_recent_project(self) -> None:
        recent_id = nicegui_app.storage.user.get("recent_project_id")
        if recent_id is None:
            return
        options = self.project_select.options or {}
        if str(recent_id) in options:
            self.project_select.value = str(recent_id)
            self.project_select.update()
            self.open_selected_project()

    def _set_select_options(self, select: ui.select, options: dict[str, str], current_value: str | None) -> None:
        value = current_value if current_value in options else (next(iter(options)) if options else None)
        select.options = options
        select.value = value
        if select is self.result_select:
            self.selected_run_id = value
        if select is self.result_rule_select:
            self.selected_result_rule_id = value
        if select is self.connection_select:
            self.selected_connection_id = value
        select.update()

    def _set_item_select_options(self, options: dict[str, str]) -> None:
        current = self.selected_item_key
        value = current if current in options else (next(iter(options)) if options else None)
        self.item_select.options = options
        self.item_select.value = value
        self.selected_item_key = value
        self.item_select.update()

    def _on_item_select_change(self, event: Any) -> None:
        self.selected_item_key = event.value
        self._highlight_overview_row()

    def _refresh_overview_view(self) -> None:
        for row in self._overview_all_rows:
            row["collapsed"] = row["stable_key"] in self._overview_collapsed
            if row["kind"] == "rule":
                row["checked"] = row["stable_key"] in self._checked_rule_keys
        self.overview_table.rows = self._visible_overview_rows()
        self.overview_table.update()
        self._highlight_overview_row()
        self._update_run_checked_button()

    def _update_run_checked_button(self) -> None:
        if not hasattr(self, "run_checked_button"):
            return
        self.run_checked_button.text = f"Run selected ({len(self._checked_rule_keys)})"
        self.run_checked_button.visible = bool(self._checked_rule_keys)
        self.run_checked_button.update()

    def _visible_overview_rows(self) -> list[dict[str, Any]]:
        query = (self.overview_search.value or "").strip().lower() if hasattr(self, "overview_search") else ""
        return self._visible_tree_rows(self._overview_all_rows, self._overview_collapsed, query)

    def _visible_results_rows(self) -> list[dict[str, Any]]:
        query = (self.results_search.value or "").strip().lower() if hasattr(self, "results_search") else ""
        return self._visible_tree_rows(
            self._results_all_rows,
            self._results_collapsed,
            query,
            search_fields=("name", "details", "last_status", "kind"),
        )

    def _visible_tree_rows(
        self,
        rows: list[dict[str, Any]],
        collapsed: set[str],
        query: str,
        search_fields: tuple[str, ...] = ("name", "details", "owner", "visibility", "used_in", "kind"),
    ) -> list[dict[str, Any]]:
        """Shared tree filtering for the Rules and Results overviews: a text search shows matches
        plus their ancestor chain (so tree context stays visible) and, for a matched group, its
        whole subtree; with no search, collapsed groups hide their descendants instead."""
        by_key = {row["stable_key"]: row for row in rows}
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("parent_key"):
                children_by_parent.setdefault(row["parent_key"], []).append(row)

        if query:
            matched = {
                row["stable_key"]
                for row in rows
                if query in " ".join(str(row.get(field, "")) for field in search_fields).lower()
            }
            visible: set[str] = set()

            def add_ancestors(row: dict[str, Any]) -> None:
                current = row
                while current is not None:
                    if current["stable_key"] in visible:
                        return
                    visible.add(current["stable_key"])
                    parent_key = current.get("parent_key")
                    current = by_key.get(parent_key) if parent_key else None

            def add_descendants(stable_key: str) -> None:
                for child in children_by_parent.get(stable_key, []):
                    if child["stable_key"] not in visible:
                        visible.add(child["stable_key"])
                        add_descendants(child["stable_key"])

            for stable_key in matched:
                add_ancestors(by_key[stable_key])
                add_descendants(stable_key)
            return [row for row in rows if row["stable_key"] in visible]

        # No search: hide any row whose ancestor chain includes a collapsed group.
        def is_hidden(row: dict[str, Any]) -> bool:
            current = row
            while current.get("parent_key"):
                parent = by_key.get(current["parent_key"])
                if parent is None:
                    return False
                if parent["stable_key"] in collapsed:
                    return True
                current = parent
            return False

        return [row for row in rows if not is_hidden(row)]

    def _on_toggle_group(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None or row.get("kind") != "group":
            return
        stable_key = row["stable_key"]
        if stable_key in self._overview_collapsed:
            self._overview_collapsed.discard(stable_key)
        else:
            self._overview_collapsed.add(stable_key)
        self._refresh_overview_view()

    def _on_toggle_rule_check(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None or row.get("kind") != "rule":
            return
        stable_key = row["stable_key"]
        if stable_key in self._checked_rule_keys:
            self._checked_rule_keys.discard(stable_key)
        else:
            self._checked_rule_keys.add(stable_key)
        self._refresh_overview_view()

    def _highlight_overview_row(self) -> None:
        selected = self.item_select.value
        self.overview_table.selected = [row for row in self.overview_table.rows if row["stable_key"] == selected]
        self.overview_table.update()

    def _select_overview_row(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None:
            return
        key = row["stable_key"]
        self.selected_item_key = key
        self.item_select.value = key
        self.item_select.update()
        self._highlight_overview_row()

    def _row_from_click_event(self, event: Any) -> dict[str, Any] | None:
        args = event.args
        if isinstance(args, (list, tuple)) and len(args) > 1 and isinstance(args[1], dict):
            return args[1]
        if isinstance(args, dict):
            row = args.get("row", args)
            return row if isinstance(row, dict) and "id" in row else None
        return None

    def _highlight_table_row(self, table: ui.table, selected_id: Any) -> None:
        selected = self._id_to_str(selected_id)
        table.selected = [row for row in table.rows if self._id_to_str(row.get("id")) == selected]
        table.update()

    def _select_connection_row(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None:
            return
        selected_id = str(row["id"])
        self.selected_connection_id = selected_id
        self.connection_select.value = selected_id
        self.connection_select.update()
        self._highlight_table_row(self.connections_table, selected_id)


    def _select_result_rule_row(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None:
            return
        key = row["stable_key"]
        self.selected_result_rule_id = key
        self.result_rule_select.value = key
        self.result_rule_select.update()
        self._highlight_results_row()
        self._populate_result_runs()
        self._update_result_trend_chart()

    def _on_result_item_select_change(self, event: Any) -> None:
        self.selected_result_rule_id = event.value
        self._highlight_results_row()
        self._populate_result_runs()
        self._update_result_trend_chart()

    def _refresh_results_view(self) -> None:
        for row in self._results_all_rows:
            row["collapsed"] = row["stable_key"] in self._results_collapsed
        self.rule_summary_table.rows = self._visible_results_rows()
        self.rule_summary_table.update()
        self._highlight_results_row()

    def _highlight_results_row(self) -> None:
        selected = self.result_rule_select.value
        self.rule_summary_table.selected = [row for row in self.rule_summary_table.rows if row["stable_key"] == selected]
        self.rule_summary_table.update()

    def _on_toggle_results_group(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None or row.get("kind") != "group":
            return
        stable_key = row["stable_key"]
        if stable_key in self._results_collapsed:
            self._results_collapsed.discard(stable_key)
        else:
            self._results_collapsed.add(stable_key)
        self._refresh_results_view()

    def _select_result_row(self, event: Any) -> None:
        row = self._row_from_click_event(event)
        if row is None:
            return
        selected_id = str(row["id"])
        self.selected_run_id = selected_id
        self.result_select.value = selected_id
        self.result_select.update()
        self._highlight_table_row(self.results_table, selected_id)
        self.view_selected_result()

    def _format_timestamp(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo:
                parsed = parsed.astimezone()
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value

    def _split_csv_text(self, value: Any) -> list[str]:
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    def _id_to_str(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    def _columns(self, labels: list[str]) -> list[dict[str, str]]:
        columns = []
        for label in labels:
            field = label.lower().replace(" ", "_")
            if label == "ID":
                field = "id"
            columns.append({"name": field, "label": label, "field": field, "align": "left"})
        return columns

    def _build_table(self, labels: list[str], pagination: int = 10) -> ui.table:
        return ui.table(
            columns=self._columns(labels),
            rows=[],
            row_key="id",
            pagination=pagination,
        ).props("flat bordered wrap-cells").classes("dq-table-wrap w-full mt-4")

    def _stat_block(
        self,
        label: str,
        value: str,
        icon: str,
        accent: str,
        background: str,
    ) -> None:
        with ui.card().classes("dq-stat-card grow basis-[190px] min-w-[170px] p-5").style(
            f"color: {accent}; background: linear-gradient(145deg, {background}, #ffffff 72%);"
        ):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().classes("gap-1"):
                    ui.label(label).classes("text-xs font-bold uppercase tracking-[0.14em] text-[#837d74]")
                    value_label = ui.label(value).classes("text-4xl font-bold leading-none text-[#37332e]")
                with ui.element("div").classes("grid w-11 h-11 rounded-xl place-items-center").style(
                    f"background: {background}; color: {accent};"
                ):
                    ui.icon(icon).classes("text-2xl")
            if label == "Connections":
                self.connections_count = value_label
            elif label == "Rules":
                self.rules_count = value_label
            elif label == "Runs":
                self.runs_count = value_label
            elif label == "Failures":
                self.failures_count = value_label

    def _set_last_action(self, message: str) -> None:
        self.last_action_label.text = message
        self.last_action_label.update()


# --- authentication -----------------------------------------------------------

LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60
_failed_login_attempts: dict[str, list[float]] = {}


def _login_blocked(username: str) -> bool:
    now = time.monotonic()
    recent = [stamp for stamp in _failed_login_attempts.get(username, []) if now - stamp < LOGIN_ATTEMPT_WINDOW_SECONDS]
    _failed_login_attempts[username] = recent
    return len(recent) >= LOGIN_ATTEMPT_LIMIT


def _record_failed_login(username: str) -> None:
    _failed_login_attempts.setdefault(username, []).append(time.monotonic())


def _list_drives() -> list[str]:
    import string

    drives = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
    return drives or ["/"]


async def pick_server_path(
    start: str | Path = "",
    directories_only: bool = True,
    extensions: tuple[str, ...] = (),
    root: str | Path | None = None,
) -> Path | None:
    """In-browser picker for the server's filesystem, so it also works for remote users.

    When `root` is given, browsing is confined to that directory and its subfolders: the
    drive selector is hidden and navigating above `root` is blocked.
    """
    root_dir: Path | None = None
    if root is not None:
        root_dir = Path(str(root)).expanduser().resolve()
        root_dir.mkdir(parents=True, exist_ok=True)

    def within_root(path: Path) -> bool:
        return root_dir is None or path == root_dir or root_dir in path.parents

    candidate = Path(str(start)).expanduser() if str(start).strip() else (root_dir or Path.home())
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.is_dir():
        candidate = root_dir or Path.home()
    candidate = candidate.resolve()
    if not within_root(candidate):
        candidate = root_dir or Path.home()
    state = {"dir": candidate}

    with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-full p-5 gap-2"):
        ui.label("Select a folder" if directories_only else "Select a file").classes("text-lg font-bold")
        path_label = ui.label("").classes("text-xs text-[#837d74] break-all")
        with ui.row().classes("w-full items-center gap-2"):
            up_button = ui.button(icon="arrow_upward", on_click=lambda: navigate(state["dir"].parent)).props(
                "flat round dense"
            ).tooltip("Up one level")
            if root_dir is None:
                drives = _list_drives()
                if len(drives) > 1:
                    ui.select(
                        drives, label="Drive", on_change=lambda event: navigate(Path(str(event.value)))
                    ).props("outlined dense").classes("w-32")
        entries = ui.column().classes("w-full gap-0 h-[300px] overflow-auto rounded border border-[#e2ded7]")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
            if directories_only:
                ui.button("Select this folder", icon="check", on_click=lambda: dialog.submit(state["dir"])).props(
                    "color=primary unelevated no-caps"
                )

    def render() -> None:
        path_label.text = str(state["dir"])
        path_label.update()
        up_button.visible = root_dir is None or state["dir"] != root_dir
        up_button.update()
        entries.clear()
        try:
            children = sorted(state["dir"].iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        except (PermissionError, OSError):
            children = []
        with entries:
            for child in children:
                if child.name.startswith(".") or child.name.startswith("$"):
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if is_dir:
                    ui.button(
                        child.name, icon="folder", on_click=lambda _=None, target=child: navigate(target)
                    ).props("flat no-caps align=left").classes("w-full justify-start")
                elif not directories_only and (not extensions or child.suffix.lower() in extensions):
                    ui.button(
                        child.name, icon="description", on_click=lambda _=None, target=child: dialog.submit(target)
                    ).props("flat no-caps align=left").classes("w-full justify-start")

    def navigate(target: Path) -> None:
        try:
            resolved = Path(target).resolve()
            if resolved.is_dir() and within_root(resolved):
                state["dir"] = resolved
        except OSError:
            return
        render()

    render()
    result = await dialog
    return result if isinstance(result, Path) else None


def open_configured_workspace() -> WorkspaceContext | None:
    root = load_settings().get("workspace_root")
    if not root:
        return None
    try:
        return open_or_create_workspace(Path(root))
    except Exception:
        return None


# --- rule scheduler -----------------------------------------------------------
#
# Runs as a single background asyncio task inside the DQTool server process (see
# `_start_scheduler` below, hooked into `nicegui.app.on_startup`). It only fires while
# that process is running — this is not an OS-level scheduled task. Every project in the
# configured workspace is polled on the same interval, independent of which projects any
# signed-in user currently has open in a browser.

SCHEDULER_POLL_SECONDS = 60


async def _execute_schedule(
    schedule: Schedule,
    project: ProjectContext,
    rules_by_id: dict[int, Rule],
    groups_by_id: dict[int, RuleGroup],
    connections: dict[int, Connection],
    execution_service: ExecutionService,
) -> None:
    status = "error"
    try:
        if schedule.target_kind == ScheduleTargetKind.RULE:
            rule = rules_by_id.get(schedule.target_id)
            rules_to_run = [rule] if rule is not None else []
        else:
            group = groups_by_id.get(schedule.target_id)
            rules_to_run = resolve_group_rules(group, groups_by_id, rules_by_id)[0] if group is not None else []
        if rules_to_run:
            runs = await nicegui_run.io_bound(
                execution_service.run_rules, rules_to_run, {}, connections, project.results_dir, "scheduler"
            )
            for run in runs:
                project.storage.save_rule_run(run)
            if any(run.status == "error" for run in runs):
                status = "error"
            elif any(run.status == "failed" for run in runs):
                status = "failed"
            else:
                status = "passed"
        # else: the scheduled rule or group was deleted since the schedule was created;
        # leave status as "error" so that's visible in the Schedules tab.
    except Exception:
        status = "error"
    next_run = compute_next_run(schedule, after=datetime.now(UTC))
    await nicegui_run.io_bound(
        project.storage.record_schedule_run, schedule.id, utc_now(), next_run.isoformat(), status
    )


async def _run_due_schedules_for_project(workspace: WorkspaceContext, project_meta: Project) -> None:
    project_dir = workspace.root_dir / project_meta.folder_name
    try:
        project = await nicegui_run.io_bound(open_or_create_project, project_dir)
    except Exception:
        return
    try:
        due = await nicegui_run.io_bound(project.storage.list_due_schedules, utc_now())
    except Exception:
        return
    if not due:
        return
    rules_by_id = {item.id: item for item in project.storage.list_rules() if item.id is not None}
    groups_by_id = {item.id: item for item in project.storage.list_rule_groups() if item.id is not None}
    connections = {item.id: item for item in project.storage.list_connections() if item.id is not None}
    execution_service = ExecutionService(ConnectorService())
    for schedule in due:
        await _execute_schedule(schedule, project, rules_by_id, groups_by_id, connections, execution_service)


async def _run_due_schedules_once() -> None:
    workspace = open_configured_workspace()
    if workspace is None:
        return
    try:
        projects = await nicegui_run.io_bound(workspace.storage.list_projects)
    except Exception:
        return
    for project_meta in projects:
        await _run_due_schedules_for_project(workspace, project_meta)


async def _scheduler_loop() -> None:
    while True:
        try:
            await _run_due_schedules_once()
        except Exception:
            pass  # a single bad poll should never kill the background loop
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


def _start_scheduler() -> None:
    asyncio.create_task(_scheduler_loop())


nicegui_app.on_startup(_start_scheduler)


def _session_user() -> tuple[WorkspaceContext, User] | None:
    """The workspace and verified account for this browser session, or None when not signed in."""
    username = nicegui_app.storage.user.get("username")
    if not username:
        return None
    workspace = open_configured_workspace()
    if workspace is None:
        return None
    user = workspace.storage.get_user(str(username))
    if user is None:
        return None
    return workspace, user


def build_login_page() -> None:
    ui.page_title("DQTool | Sign in")
    settings = load_settings()
    configured_root = settings.get("workspace_root") or ""

    async def browse_folder() -> None:
        selected = await pick_server_path(str(workspace_input.value or ""), directories_only=True)
        if selected is None:
            return
        workspace_input.value = str(selected)
        workspace_input.update()
        apply_workspace()

    def apply_workspace() -> None:
        path_text = (workspace_input.value or "").strip()
        if not path_text:
            ui.notify("Enter a workspace folder path first.", type="warning")
            return
        try:
            open_or_create_workspace(Path(path_text))
        except Exception as exc:
            ui.notify(f"Could not open that folder: {exc}", type="negative")
            return
        current = load_settings()
        current["workspace_root"] = str(Path(path_text))
        save_settings(current)
        workspace_label.text = f"Workspace: {path_text}"
        workspace_label.update()
        ui.notify("Workspace saved. You can sign in now.", type="positive")

    async def try_sign_in() -> None:
        username = (username_input.value or "").strip()
        password = password_input.value or ""
        if not username or not password:
            ui.notify("Enter your username and password.", type="warning")
            return
        if _login_blocked(username):
            ui.notify("Too many failed attempts. Wait a minute and try again.", type="negative")
            return
        workspace = open_configured_workspace()
        if workspace is None:
            ui.notify("Select a workspace folder first.", type="warning")
            return
        user = await nicegui_run.io_bound(workspace.storage.verify_login, username, password)
        if user is None:
            _record_failed_login(username)
            ui.notify("Invalid username or password.", type="negative")
            return
        nicegui_app.storage.user.update({"username": user.username})
        ui.navigate.to("/")

    with ui.column().classes("w-full items-center justify-center min-h-screen bg-[#f6f4f0]"):
        with ui.card().classes("w-[460px] max-w-full p-8 gap-3"):
            with ui.row().classes("items-center gap-3"):
                ui.html(COLRUYT_LOGO_SVG)
                with ui.column().classes("gap-0"):
                    ui.label("DQTool").classes("text-2xl font-bold tracking-tight text-[#37332e]")
                    ui.label("COLRUYT GROUP").classes("text-[9px] font-bold tracking-[0.22em] text-[#837d74]")
            ui.label("Sign in").classes("text-xl font-bold text-[#37332e] mt-2")
            workspace_label = ui.label(
                f"Workspace: {configured_root}" if configured_root else "No workspace selected yet."
            ).classes("text-xs text-[#837d74]")
            with ui.expansion("Workspace folder", icon="folder").classes("w-full") as workspace_section:
                workspace_input = ui.input(
                    "Workspace folder",
                    value=configured_root,
                    placeholder=r"C:\Users\<you>\Documents\DQToolWorkspace",
                ).props("outlined dense").classes("w-full")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Browse", icon="folder", on_click=browse_folder).props("outline no-caps color=primary")
                    ui.button("Use this folder", icon="check", on_click=apply_workspace).props(
                        "unelevated no-caps color=primary"
                    )
            workspace_section.value = not configured_root
            username_input = ui.input("Username").props("outlined dense").classes("w-full")
            password_input = ui.input("Password", password=True, password_toggle_button=True).props(
                "outlined dense"
            ).classes("w-full")
            password_input.on("keydown.enter", try_sign_in)
            ui.button("Sign in", icon="login", on_click=try_sign_in).props("unelevated no-caps color=primary").classes(
                "w-full mt-2"
            )


@ui.page("/")
def index_page() -> RedirectResponse | None:
    session = _session_user()
    if session is None:
        return RedirectResponse("/login")
    workspace, user = session
    DQToolWebApp(workspace=workspace, user=user).build()
    return None


@ui.page("/login")
def login_page() -> RedirectResponse | None:
    if _session_user() is not None:
        return RedirectResponse("/")
    build_login_page()
    return None


def main() -> int:
    host = os.environ.get("DQTOOL_HOST", "127.0.0.1")
    port = int(os.environ.get("DQTOOL_PORT", "8080"))
    original_setup = nicegui_run.setup

    def safe_setup() -> None:
        try:
            original_setup()
        except PermissionError:
            nicegui_run.process_pool = None

    nicegui_run.setup = safe_setup
    run_kwargs: dict[str, Any] = {}
    ssl_certfile = os.environ.get("DQTOOL_SSL_CERTFILE")
    ssl_keyfile = os.environ.get("DQTOOL_SSL_KEYFILE")
    if ssl_certfile and ssl_keyfile:
        # Serve HTTPS directly so passwords are encrypted on the network.
        run_kwargs["ssl_certfile"] = ssl_certfile
        run_kwargs["ssl_keyfile"] = ssl_keyfile
    ui.run(
        host=host,
        port=port,
        title="DQTool",
        reload=False,
        show=False,
        storage_secret=get_or_create_storage_secret(),
        **run_kwargs,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
