"""Textual TCSS Stylesheet for Presence Terminal Control Center
Defines responsive multi-panel layout, theme colors, cards, and status badges.
"""

TUI_CSS = """
Screen {
    layout: vertical;
    background: $surface;
    color: $text;
}

#header-bar {
    dock: top;
    height: 3;
    background: $panel;
    border-bottom: solid $primary;
    padding: 0 1;
}

#main-container {
    height: 1fr;
    layout: horizontal;
}

#nav-sidebar {
    width: 24;
    height: 100%;
    background: $panel-darken-1;
    border-right: solid $panel-lighten-2;
    padding: 0;
}

.nav-title {
    text-style: bold;
    color: $accent;
    padding: 1 1 0 1;
    margin-bottom: 0;
}

.mode-badge-local {
    background: $success;
    color: black;
    text-style: bold;
    text-align: center;
    padding: 0 1;
    margin: 1 1 0 1;
}

.observing-badge-active {
    background: $accent;
    color: black;
    text-style: bold;
    text-align: center;
    padding: 0 1;
    margin: 0 1 1 1;
}

#nav-buttons {
    height: auto;
}

#nav-buttons Button {
    width: 100%;
    height: 1;
    border: none;
    margin: 0;
    padding: 0 1;
    text-align: left;
}

.recent-chats-title {
    text-style: bold;
    color: $secondary;
    padding: 1 1 0 1;
    border-top: solid $panel-lighten-1;
}

#recent-chats-list {
    height: 1fr;
    padding: 0 1;
}

#workspace {
    width: 1fr;
    height: 100%;
    padding: 0 1;
}

#workspace-switcher {
    width: 100%;
    height: 100%;
}

#workspace-switcher > * {
    width: 100%;
    height: 100%;
}

#activity-sidebar {
    width: 32;
    height: 100%;
    background: $panel-darken-1;
    border-left: solid $panel-lighten-2;
    padding: 1 1;
}

.panel-header {
    text-style: bold;
    color: $primary;
    border-bottom: solid $primary-darken-2;
    margin-bottom: 1;
}

/* Chat View */
#chat-view-container {
    height: 100%;
    layout: vertical;
}

#chat-messages-scroll {
    height: 1fr;
    border: solid $panel-lighten-2;
    padding: 1;
    overflow-y: scroll;
}

.chat-msg-user {
    color: $accent;
    margin-bottom: 1;
    padding: 0 1;
    background: $panel-lighten-1;
    border-left: solid $accent;
}

.chat-msg-agent {
    color: $text;
    margin-bottom: 1;
    padding: 0 1;
    border-left: solid $primary;
}

.tool-invocation-card {
    border: round $secondary;
    background: $panel-darken-2;
    padding: 0 1;
    margin: 1 0;
}

#chat-input-bar {
    height: 3;
    margin-top: 1;
    layout: horizontal;
}

#chat-input {
    width: 1fr;
}

#chat-send-btn {
    width: 12;
    min-width: 10;
    margin-left: 1;
}

/* Explorer View */
#explorer-container {
    height: 100%;
    layout: horizontal;
}

#explorer-left {
    width: 35;
    height: 100%;
    border-right: solid $panel-lighten-2;
    padding-right: 1;
}

#explorer-right {
    width: 1fr;
    height: 100%;
    padding-left: 1;
}

/* MCP View */
#mcp-container {
    height: 100%;
    layout: vertical;
}

.mcp-actions-bar {
    height: 3;
    margin-bottom: 1;
}

.mcp-actions-bar Button {
    margin-right: 1;
}

#mcp-table-container {
    height: 12;
    border: solid $panel-lighten-2;
    margin-bottom: 1;
}

#mcp-details-container {
    height: 1fr;
    border: solid $panel-lighten-2;
    padding: 1;
    overflow-y: scroll;
}

/* Task Monitor View */
#tasks-container {
    height: 100%;
    layout: horizontal;
}

#tasks-tree-container {
    width: 40;
    height: 100%;
    border-right: solid $panel-lighten-2;
}

#tasks-details-container {
    width: 1fr;
    height: 100%;
    padding: 1;
    overflow-y: scroll;
}

/* Trace View */
#trace-main-container {
    height: 100%;
    layout: vertical;
}

.trace-nav-bar {
    height: 3;
    margin-bottom: 1;
}

.trace-nav-bar Button {
    margin-right: 1;
}

#trace-turn-label {
    width: auto;
    padding: 0 1;
    content-align: center middle;
}

#trace-container {
    height: 1fr;
    layout: horizontal;
}

#trace-timeline-container {
    width: 45;
    height: 100%;
    border-right: solid $panel-lighten-2;
}

#trace-detail-container {
    width: 1fr;
    height: 100%;
    padding: 1;
    overflow-y: scroll;
}

/* Context Inspector View */
#context-container {
    height: 100%;
    layout: vertical;
    overflow-y: scroll;
    padding: 1;
}

.context-section {
    border: solid $panel-lighten-1;
    padding: 1;
    margin-bottom: 1;
}

/* System Health View */
#health-container {
    height: 100%;
    layout: vertical;
    overflow-y: scroll;
    padding: 1;
}

.health-grid {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    height: auto;
    margin-bottom: 1;
}

.health-card {
    border: round $primary;
    padding: 1;
    background: $panel;
    height: auto;
}

/* Footer */
#footer-bar {
    dock: bottom;
    height: 1;
    background: $panel-darken-2;
    color: $text-muted;
    padding: 0 1;
}

/* Settings / API Key Modal */
ApiKeyModal {
    align: center middle;
}

#api-key-dialog {
    width: 76;
    height: auto;
    border: thick $primary;
    background: $panel;
    padding: 1 2;
}


#api-key-dialog-title {
    text-style: bold;
    color: $accent;
    border-bottom: solid $primary;
    margin-bottom: 1;
}

.dialog-desc {
    color: $text-muted;
    margin-bottom: 1;
}

.dialog-label {
    margin-top: 1;
    text-style: bold;
    color: $text;
}

#api-key-status {
    height: auto;
    min-height: 1;
    margin-top: 1;
    margin-bottom: 1;
}

#dialog-buttons {
    height: auto;
    margin-top: 1;
    align: right middle;
}

#dialog-buttons Button {
    margin-left: 1;
}

#chat-configure-key-btn {
    margin: 1 0;
    width: auto;
}

#consent-dialog {
    width: 80;
    max-height: 85%;
    border: thick $error;
    background: $panel;
    padding: 1 2;
}

#consent-dialog-title {
    text-style: bold;
    color: $error;
    border-bottom: solid $error;
    margin-bottom: 1;
}

#consent-details-scroll {
    height: auto;
    max-height: 18;
    background: $surface;
    padding: 1;
    margin: 1 0;
    border: round $primary-background;
}

.consent-item-bold {
    text-style: bold;
    color: $text;
}

.consent-item {
    color: $text-muted;
}

.consent-item-reason {
    color: $warning;
    text-style: italic;
    margin-top: 1;
}

.consent-param-title {
    text-style: bold;
    margin-top: 1;
    color: $accent;
}

.consent-params-box {
    background: $background;
    padding: 0 1;
    color: $text-muted;
}

#consent-expiry-notice {
    color: $warning;
    text-style: dim;
    margin: 1 0;
}
"""
