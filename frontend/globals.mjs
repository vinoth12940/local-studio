// The page's globals, set in one place (refactor P2). There are no inline event handlers (the CSP refuses them): use delegated
// listeners. window.LS is a console handle for debugging (CLAUDE.md: render synthetic turns into LS.current, then
// LS.renderThread()).
import * as main from "./main.mjs";
import "./modules.mjs";                                   // the modules run in their original order first
import * as core_util from "./core/util.mjs";
import * as core_markdown from "./core/markdown.mjs";
import * as ui_theme from "./ui/theme.mjs";
import * as ui_views from "./ui/views.mjs";
import * as ui_engine from "./ui/engine.mjs";
import * as chat_list from "./chat/list.mjs";
import * as chat_thread from "./chat/thread.mjs";
import * as chat_attachments from "./chat/attachments.mjs";
import * as chat_composer from "./chat/composer.mjs";
import * as chat_live from "./chat/live.mjs";
import * as chat_computer from "./chat/computer.mjs";
import * as images_images from "./images/images.mjs";
import * as code_editor from "./code/editor.mjs";
import * as code_terminal from "./code/terminal.mjs";
import * as library_library from "./library/library.mjs";
import * as settings_settings from "./settings/settings.mjs";
import * as search_search from "./search/search.mjs";
import * as agent_helpers from "./agent/helpers.mjs";
import * as agent_state from "./agent/state.mjs";
import * as agent_agent from "./agent/agent.mjs";
import * as settings_agent from "./settings/agent.mjs";

const SPACES = [main, core_util, core_markdown, ui_theme, ui_views, ui_engine, chat_list, chat_thread, chat_attachments, chat_composer, chat_live, chat_computer, images_images, code_editor, code_terminal, library_library, settings_settings, search_search, agent_helpers, agent_state, agent_agent, settings_agent];
const STORES = () => [chat_attachments.attachmentsState, chat_list.listState, chat_thread.threadState, library_library.libraryState, search_search.searchState];            // names more than one module assigns live in these
window.LS = new Proxy({}, {
  get: (_, k) => STORES().find((s) => k in s)?.[k] ?? SPACES.map((m) => m[k]).find((v) => v !== undefined),
  set: (_, k, v) => { const s = STORES().find((x) => k in x); if (!s) return false; s[k] = v; return true; },
});
