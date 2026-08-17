// =========================================================================
// Coinche CLI Online web overlay (U3)
//
// A Vue 3 single-file (no build step) front-end that renders entirely from the
// state snapshots pushed by U2 over the WebSocket and sends action frames back.
//
// HARD RULES honored here:
//  - Browser has NO authority: legality (legal cards / legal bids), trick
//    winner, and scoring come only from the snapshot — never computed in JS.
//  - Anti-XSS: every untrusted string (player names, team labels, chat text) is
//    rendered through Vue's {{ }} / :text bindings, which auto-escape. We NEVER
//    use v-html on those. (see ChatPanel / SeatPanel / TeamPicker)
//  - Full-replace rendering: each {type:"state"} frame carries the complete
//    snapshot; we replace the whole reactive object (idempotent, no deltas).
//
// The action verbs sent over the WS are exactly U2's WebActionProtocol names:
//   play | bid | chat | join | continue | rematch | lobby | fill_bots   (card play is "play", NOT
//   "play_card" — play_card is the game-wire type, not the browser action).
// =========================================================================

const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

// ---- Card vocabulary (display + accessible names) -----------------------
const SUIT_NAMES = { "♥": "Cœur", "♦": "Carreau", "♠": "Pique", "♣": "Trèfle" };
const RED_SUITS = new Set(["♥", "♦"]);
const RANK_NAMES = {
  7: "7",
  8: "8",
  9: "9",
  10: "10",
  V: "Valet",
  D: "Dame",
  R: "Roi",
  A: "As",
};

// Seat rotation — a faithful port of coinche/ui.py `_visual_position`, so the
// browser and terminal agree on where each seat renders (local seat = south).
const ROTATION = ["N", "W", "S", "E"];
const VISUAL_SLOTS = ["south", "east", "north", "west"];
function visualPosition(seat, localSeat) {
  if (!seat || !localSeat) return "south";
  const offset = (ROTATION.indexOf(seat) - ROTATION.indexOf(localSeat) + 4) % 4;
  return VISUAL_SLOTS[offset];
}

// Split a card string like "10♥" / "V♠" into { rank, suit }.
function splitCard(card) {
  if (!card) return { rank: "", suit: "" };
  return { rank: card.slice(0, -1), suit: card.slice(-1) };
}
function cardLabel(card) {
  const { rank, suit } = splitCard(card);
  const r = RANK_NAMES[rank] || rank;
  const s = SUIT_NAMES[suit] || suit;
  return `${r} de ${s}`;
}

const REDUCED_MOTION = window.matchMedia(
  "(prefers-reduced-motion: reduce)",
).matches;

// Server table keys are ASCII alphanumeric and capped at 20 characters.
// The catalogue is shared with the Python server and terminal client.
const TABLE_KEY_MAX_LENGTH = 20;
let tableNames = [];

async function loadTableNames() {
  try {
    const response = await fetch("/table_names.json");
    const names = await response.json();
    if (response.ok && Array.isArray(names) && names.every((name) => typeof name === "string")) {
      tableNames = names;
    }
  } catch {
    // The manual table-name field remains usable if the static asset is unavailable.
  }
}

// =========================================================================
// Card (SVG) — vector face, crisp at any size, accessible name.
// Renders visual only; interactivity (click/keyboard) is layered by HandFan.
// =========================================================================
const Card = {
  props: {
    card: { type: String, default: null },
    faceUp: { type: Boolean, default: true },
    legal: { type: Boolean, default: false },
    illegal: { type: Boolean, default: false },
    shake: { type: Boolean, default: false },
    pending: { type: Boolean, default: false },
    interactive: { type: Boolean, default: false },
    trump: { type: String, default: null },
  },
  emits: ["play"],
  computed: {
    parts() {
      return splitCard(this.card);
    },
    isRed() {
      return RED_SUITS.has(this.parts.suit);
    },
    isTrump() {
      return this.trump != null && this.faceUp && this.parts.suit === this.trump;
    },
    label() {
      return this.faceUp ? cardLabel(this.card) : "Carte face cachée";
    },
    classes() {
      return {
        "card--legal": this.legal,
        "card--illegal": this.illegal,
        "card--shake": this.shake,
        "card--pending": this.pending,
        "card--trump": this.isTrump,
      };
    },
  },
  methods: {
    onActivate() {
      if (this.interactive) this.$emit("play", this.card);
    },
  },
  // role/aria-label give the SVG an accessible name ("Valet de Cœur"); when
  // interactive we also expose it as a button for keyboard play (Enter/Space).
  template: `
    <div
      class="card"
      :class="classes"
      :role="interactive ? 'button' : 'img'"
      :aria-label="label"
      :tabindex="interactive ? 0 : -1"
      :data-testid="interactive ? 'card' : null"
      :data-card="card"
      @click="onActivate"
      @keydown.enter.prevent="onActivate"
      @keydown.space.prevent="onActivate"
    >
      <svg viewBox="0 0 66 96" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <template v-if="faceUp">
          <rect x="1" y="1" width="64" height="94" rx="7" fill="var(--card-face)"
                stroke="rgba(0,0,0,0.25)" stroke-width="1" />
          <g :fill="isRed ? 'var(--suit-red)' : 'var(--suit-black)'"
             font-family="Georgia, 'Times New Roman', serif" font-weight="700">
            <text x="7" y="18" font-size="15" text-anchor="start">{{ parts.rank }}</text>
            <text x="7" y="31" font-size="13" text-anchor="start">{{ parts.suit }}</text>
            <text x="33" y="60" font-size="34" text-anchor="middle">{{ parts.suit }}</text>
            <g transform="rotate(180 33 48)">
              <text x="7" y="18" font-size="15" text-anchor="start">{{ parts.rank }}</text>
              <text x="7" y="31" font-size="13" text-anchor="start">{{ parts.suit }}</text>
            </g>
          </g>
        </template>
        <template v-else>
          <rect x="1" y="1" width="64" height="94" rx="7" fill="#0a2a1c"
                stroke="var(--gold)" stroke-width="1.5" />
          <rect x="6" y="6" width="54" height="84" rx="4" fill="none"
                stroke="rgba(212,175,55,0.5)" stroke-width="1"
                stroke-dasharray="4 3" />
          <text x="33" y="56" font-size="26" text-anchor="middle" fill="var(--gold-soft)"
                aria-hidden="true">♣</text>
        </template>
      </svg>
      <div v-if="pending" class="card__spinner" aria-label="En attente">
        <svg class="card__spinner-svg" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <circle cx="12" cy="12" r="10" fill="none" stroke="var(--gold)" stroke-width="2.5"
                  stroke-dasharray="31.4 31.4" stroke-linecap="round" />
        </svg>
      </div>
    </div>
  `,
};

// =========================================================================
// SeatPanel — one player position around the felt.
// Player name via {{ }} (auto-escaped) — anti-XSS hard rule.
// =========================================================================
const SeatPanel = {
  components: { Card },
  props: {
    pos: String, // south|east|north|west
    name: String,
    teamClass: String, // nous|eux
    playedCard: { type: String, default: null },
    bidMark: { type: String, default: null },
    isTurn: Boolean,
    isDealer: Boolean,
    isBot: Boolean,
    botType: { type: String, default: null },
    connected: { type: Boolean, default: true },
    trump: { type: String, default: null },
  },
  computed: {
    seatClasses() {
      return [
        "seat",
        "seat--" + this.pos,
        "seat--" + this.teamClass,
        { "seat--turn": this.isTurn, "seat--offline": !this.connected },
      ];
    },
    isPass() {
      return this.bidMark === "Passe";
    },
  },
  template: `
    <div :class="seatClasses">
      <div class="seat__identity">
        <div class="seat__nameplate">
          <span class="seat__name">{{ name }}</span>
          <span v-if="isBot && isTurn" class="seat__turn-spinner" role="status" aria-label="Le bot joue"></span>
          <span v-if="isDealer" class="seat__badge">(D)</span>
        </div>
        <button v-if="isBot" class="seat__tag" type="button"
          :aria-label="'Changer le type du bot ' + name"
          :title="'Changer le type du bot'"
          @click="$emit('change-bot-type')">BOT {{ (botType || 'smart') }}</button>
      </div>
      <span v-if="!connected" class="seat__offline-note">déconnecté</span>
      <div class="seat__slot">
        <card v-if="playedCard" :card="playedCard" :trump="trump"></card>
        <span v-else-if="bidMark" class="bid-mark" :class="{ 'bid-mark--pass': isPass }">{{ bidMark }}</span>
      </div>
    </div>
  `,
};

// =========================================================================
// BidPanel — role="dialog" overlay shown on the local player's bid turn.
// Every option comes from the snapshot's pending_bid_request (no legality in JS).
// =========================================================================
const BidPanel = {
  props: {
    request: Object, // { legal_actions, current_highest_bid, can_coinche, can_surcoinche }
    sending: Boolean,
  },
  emits: ["bid"],
  data() {
    // Replié par défaut : on voit ses cartes tout de suite, et « Passe » reste
    // accessible sans ouvrir le panneau. On n'ouvre que pour annoncer/coincher.
    return { selectedTrump: null, pointsIndex: 0, collapsed: true };
  },
  computed: {
    legalActions() {
      return (this.request && this.request.legal_actions) || [];
    },
    trumps() {
      const seen = [];
      for (const a of this.legalActions)
        if (!seen.includes(a.trump)) seen.push(a.trump);
      return seen;
    },
    pointsForTrump() {
      // Numeric levels only. Capot is announced via its own dedicated button
      // (see capotAvailable / announceCapot), not by stepping past 180 — it's a
      // distinct, higher-value contract, not just the next number up.
      if (!this.selectedTrump) return [];
      return this.legalActions
        .filter((a) => a.trump === this.selectedTrump && a.points !== "capot")
        .map((a) => a.points)
        .sort((a, b) => a - b);
    },
    currentPoints() {
      return this.pointsForTrump[this.pointsIndex];
    },
    currentPointsLabel() {
      return this.currentPoints;
    },
    capotOffered() {
      // Whether capot is still on the table at all this auction (it's gone once
      // someone has already announced it). Drives whether we show the button.
      return this.legalActions.some((a) => a.points === "capot");
    },
    canAnnounceCapot() {
      // Capot is bound to a trump suit, so a suit must be chosen first — same
      // as a numeric announce. The button stays visible (for discoverability)
      // but is disabled until then.
      return (
        this.selectedTrump != null &&
        this.legalActions.some((a) => a.points === "capot" && a.trump === this.selectedTrump)
      );
    },
    canAnnounce() {
      return this.selectedTrump != null && this.currentPoints != null;
    },
    highestLabel() {
      const b = this.request && this.request.current_highest_bid;
      if (!b) return "aucune";
      const p = b.points === "capot" ? "Capot" : b.points;
      return `${p} ${b.trump}`;
    },
  },
  methods: {
    isRed(suit) {
      return RED_SUITS.has(suit);
    },
    pickTrump(t) {
      this.selectedTrump = t;
      this.pointsIndex = 0;
    },
    step(delta) {
      const n = this.pointsForTrump.length;
      if (!n) return;
      this.pointsIndex = Math.min(Math.max(this.pointsIndex + delta, 0), n - 1);
    },
    announce() {
      if (!this.canAnnounce) return;
      this.$emit("bid", {
        bid_action: "bid",
        trump: this.selectedTrump,
        points: this.currentPoints,
      });
    },
    announceCapot() {
      if (!this.canAnnounceCapot) return;
      this.$emit("bid", {
        bid_action: "bid",
        trump: this.selectedTrump,
        points: "capot",
      });
    },
    pass() {
      this.$emit("bid", { bid_action: "pass" });
    },
    coinche() {
      this.$emit("bid", { bid_action: "coinche" });
    },
    surcoinche() {
      this.$emit("bid", { bid_action: "surcoinche" });
    },
    toggleCollapsed() {
      // Replier le panneau libère la vue sur la main (surtout en mobile, où
      // il occupe le bas de l'écran) sans quitter l'enchère en cours.
      this.collapsed = !this.collapsed;
    },
  },
  mounted() {
    // Focus into the dialog (a11y: BidPanel role=dialog with focus handling).
    // Mais si le joueur est en train d'écrire (chat, saisie de nom…), on ne
    // vole PAS le focus : sinon la touche Entrée/Espace qu'il tape activerait
    // aussitôt « Passe »/« Annoncer » (passe ou annonce tout seul).
    nextTick(() => {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      const first = this.$el.querySelector("button:not(:disabled)");
      if (first) first.focus();
    });
  },
  template: `
    <!-- Replié : une barre flottante qui laisse voir la main dessous. « Passe »
         est directement accessible ; « Annoncer » rouvre le panneau. -->
    <div v-if="collapsed" class="bid-collapsed-bar">
      <button class="bid-collapsed-btn bid-collapsed-btn--pass" data-testid="bid-pass-collapsed"
              :disabled="sending" @click="pass">Passe</button>
      <button class="bid-collapsed-btn bid-collapsed-btn--reopen" data-testid="bid-reopen"
              :disabled="sending" @click="toggleCollapsed" aria-label="Ouvrir l'annonce">
        Annoncer <span class="bid-reopen__chevron" aria-hidden="true">▲</span>
      </button>
    </div>
    <div v-else class="scrim">
      <div class="bid-panel" :class="{ 'bid-panel--sending': sending }"
           role="dialog" aria-modal="true" aria-labelledby="bid-title">
        <div class="bid-panel__header">
          <h2 class="bid-panel__title" id="bid-title">À vous d'annoncer</h2>
          <button class="bid-collapse" data-testid="bid-collapse" @click="toggleCollapsed"
                  aria-label="Masquer pour voir mes cartes">
            <span aria-hidden="true">▼</span>
          </button>
        </div>
        <p class="bid-panel__legend">Enchère actuelle : {{ highestLabel }}</p>

        <div class="bid-panel__group">
          <div class="bid-panel__legend">Atout</div>
          <div class="trump-buttons">
            <button
              v-for="suit in ['♥','♠','♦','♣']"
              :key="suit"
              class="trump-btn"
              :class="{ 'trump-btn--selected': selectedTrump === suit }"
              :data-suit="isRed(suit) ? 'red' : 'black'"
              :data-testid="'bid-trump-' + suit"
              :disabled="!trumps.includes(suit) || sending"
              @click="pickTrump(suit)"
            >{{ suit }}</button>
          </div>
        </div>

        <div class="bid-panel__group" v-if="selectedTrump">
          <div class="bid-panel__legend">Points</div>
          <div class="points-stepper">
            <button class="stepper-btn" data-testid="bid-points-down"
                    :disabled="pointsIndex <= 0 || sending" @click="step(-1)"
                    aria-label="Diminuer les points">−</button>
            <span class="points-value" data-testid="bid-points">{{ currentPointsLabel }}</span>
            <button class="stepper-btn" data-testid="bid-points-up"
                    :disabled="pointsIndex >= pointsForTrump.length - 1 || sending" @click="step(1)"
                    aria-label="Augmenter les points">+</button>
          </div>
        </div>

        <div class="bid-panel__actions">
          <button class="action-btn action-btn--pass" data-testid="bid-pass"
                  :disabled="sending" @click="pass">Passe</button>
          <button class="action-btn action-btn--announce" data-testid="bid-announce"
                  :disabled="!canAnnounce || sending" @click="announce">Annoncer</button>
          <button v-if="capotOffered" class="action-btn action-btn--capot" data-testid="bid-capot"
                  :disabled="!canAnnounceCapot || sending" @click="announceCapot">Capot</button>
          <button v-if="request && request.can_coinche" class="action-btn action-btn--coinche"
                  data-testid="bid-coinche" :disabled="sending" @click="coinche">Coincher</button>
          <button v-if="request && request.can_surcoinche" class="action-btn action-btn--surcoinche"
                  data-testid="bid-surcoinche" :disabled="sending" @click="surcoinche">Surcoincher</button>
        </div>
        <p v-if="sending" class="bid-panel__sending">envoi…</p>
      </div>
    </div>
  `,
};

// =========================================================================
// ChatPanel — collapsible. Names & text via {{ }} (auto-escaped, anti-XSS).
// =========================================================================
const ChatPanel = {
  props: {
    messages: Array,
    systemMessages: Array,
    localTeam: String, // NS | EW
    draft: { type: String, default: "" },
  },
  emits: ["send", "close", "update:draft"],
  computed: {
    humanMessages() {
      return (this.messages || []).map((m) => ({
        name: m.name,
        text: m.text,
        cls: m.team === this.localTeam ? "nous" : "eux",
        time: new Date(m.ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }),
      }));
    },
    announcements() {
      return (this.systemMessages || []).map((m) => ({
        name: m.name,
        text: m.text,
        cls: m.team === this.localTeam ? "nous" : "eux",
        time: new Date(m.ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }),
      }));
    },
  },
  methods: {
    scrollToNewest(sectionClass) {
      const log = this.$el.querySelector(sectionClass);
      if (log) log.scrollTop = log.scrollHeight;
    },
    submit() {
      const text = this.draft.trim().slice(0, 256); // UX cap only; server is authoritative
      if (!text) return;
      this.$emit("send", text);
      this.$emit("update:draft", "");
    },
  },
  mounted() {
    // Opening the chat starts at the newest exchange, including from a round
    // recap or game-over screen.
    nextTick(() => {
      this.scrollToNewest(".chat-section--announcements");
      this.scrollToNewest(".chat-section--messages");
      const input = this.$el.querySelector(".chat-input");
      if (input) input.focus();
    });
  },
  watch: {
    humanMessages(messages, previousMessages) {
      if (messages.length > (previousMessages?.length || 0)) {
        nextTick(() => this.scrollToNewest(".chat-section--messages"));
      }
    },
    announcements(messages, previousMessages) {
      if (messages.length > (previousMessages?.length || 0)) {
        nextTick(() => this.scrollToNewest(".chat-section--announcements"));
      }
    },
  },
  template: `
    <aside class="chat-panel" role="region" aria-label="Discussion">
      <div class="chat-panel__header">
        <span>Discussion</span>
        <button class="chat-panel__close" aria-label="Fermer la discussion" @click="$emit('close')">×</button>
      </div>
      <div class="chat-log" aria-live="polite">
        <section class="chat-section chat-section--announcements">
          <h3 class="chat-section__title">Annonces système</h3>
          <p v-if="!announcements.length" class="chat-empty">Aucune annonce.</p>
          <div v-for="(m, i) in announcements" :key="'system-' + i" class="chat-msg chat-msg--system">
            <time class="chat-msg__time">{{ m.time }}</time>
            <span v-if="m.name !== 'Système'" class="chat-msg__name" :class="'chat-msg__name--' + m.cls">{{ m.name }}</span>
            <span class="chat-msg__text">{{ m.text }}</span>
          </div>
        </section>
        <section class="chat-section chat-section--messages">
          <h3 class="chat-section__title">Messages</h3>
          <p v-if="!humanMessages.length" class="chat-empty">Aucun message.</p>
          <div v-for="(m, i) in humanMessages" :key="'chat-' + i" class="chat-msg">
            <time class="chat-msg__time">{{ m.time }}</time>
            <span class="chat-msg__name" :class="'chat-msg__name--' + m.cls">{{ m.name }}</span>
            <span class="chat-msg__text">{{ m.text }}</span>
          </div>
        </section>
      </div>
      <form class="chat-compose" @submit.prevent="submit">
         <input class="chat-input" type="text" maxlength="256" :value="draft"
           @input="$emit('update:draft', $event.target.value)"
               placeholder="Message…" aria-label="Votre message" />
        <button class="chat-send" type="submit" data-testid="chat-send">Envoyer</button>
      </form>
    </aside>
  `,
};

// =========================================================================
// Root application
// =========================================================================
const App = {
  components: { Card, SeatPanel, BidPanel, ChatPanel },
  setup() {
    // -------- reactive state --------
    const snapshot = ref(null); // latest full snapshot (source of truth)
    const toasts = ref([]); // transient messages
    const chatOpen = ref(window.innerWidth >= 1024); // docked open on desktop
    // This belongs to App, not ChatPanel: the round recap temporarily unmounts
    // the panel, but must never discard a message currently being composed.
    const chatDraft = ref("");
    const unread = ref(0);
    const bidSending = ref(false);
    const fillingBots = ref(false);
    const leaveArmed = ref(false); // mid-game leave needs a 2nd click to confirm
    let leaveDisarmTimer = null;
    const leaving = ref(false); // "leave" sent, waiting for the server to return us to the lobby
    let leavingTimer = null;
    const shakeCard = ref(null);
    const pendingCard = ref(null); // card pre-selected while waiting for our turn
    const dealing = ref(false);
    const badgeFlash = ref(false);
    const bidEffect = ref(null);
    const bidEffectKey = ref(0);
    const beloteEffect = ref(null);
    const beloteEffectKey = ref(0);
    const joinEffect = ref(null);
    const joinEffectKey = ref(0);
    const redealEffect = ref(false);
    const redealEffectKey = ref(0);
    const bidAnnouncement = ref(null);
    const bidAnnouncementKey = ref(0);
    const sweepClass = ref(null); // e.g. "sweep-north" while a trick sweeps out
    const confetti = ref([]);
    // True while a *dropped* WebSocket is being recovered. Drives the
    // full-screen "reconnexion…" overlay that blocks input — so a player
    // returning to a backgrounded tab sees clearly that clicks won't register
    // yet, instead of tapping into the void while the socket comes back. Starts
    // false: the very first connect is covered by the lobby's own loading
    // spinner, so we only raise this on an actual drop (see scheduleReconnect).
    const reconnecting = ref(false);
    const countdownNow = ref(Date.now() / 1000);
    let countdownInterval = null;
    let countdownWarnings = new Set();
    // Optional méta-client context: when this page is served by the multi-
    // session méta-client, `window.__META__` carries the per-session WebSocket
    // path and the name the player already chose on the landing page. In the
    // mono-session overlay it's absent, so everything falls back to the
    // original behaviour (root `/ws`, empty name).
    const META = window.__META__ || {};
    const isMetaClient = META.metaClient === true;
    const PENDING_JOIN_KEY = "coinche.pendingJoin";

    // Session recovery (méta-client only): persist this session's id so the
    // landing page can bring the player straight back here after a refresh or
    // an accidental tab close (see the landing-page script in meta/server.py).
    // Absent in the mono-session overlay (no sessionId), where it's a no-op.
    if (META.sessionId) {
      try {
        window.localStorage.setItem("coinche.metaSessionId", META.sessionId);
      } catch (e) {
        /* localStorage unavailable (private mode) — recovery just won't persist */
      }
    }

    // Last name the player used, persisted separately from the session id so it
    // survives a dead/expired session. On the méta-client the landing page
    // already carries the name over via META.name; this is the fallback for the
    // mono-session overlay (no landing page — the lobby below IS the home page).
    const LAST_NAME_KEY = "coinche.lastName";
    function readLastName() {
      try {
        return window.localStorage.getItem(LAST_NAME_KEY) || "";
      } catch (e) {
        return ""; // localStorage unavailable (private mode)
      }
    }
    function rememberName(name) {
      const value = (name || "").trim();
      if (!value) return;
      try {
        window.localStorage.setItem(LAST_NAME_KEY, value);
      } catch (e) {
        /* localStorage unavailable (private mode) — just don't persist */
      }
    }

    // Lobby form (there is no table-list in the snapshot contract; U2 pushes
    // players/status only — so the lobby is a join form driven by that state).
    const lobby = reactive({ name: META.name || readLastName(), table: META.tableKey || "table1", team: "" });

    function readPendingJoin() {
      try {
        const pending = JSON.parse(window.localStorage.getItem(PENDING_JOIN_KEY) || "null");
        if (pending && /^[A-Za-z0-9]{4,20}$/.test(pending.tableKey || "")) {
          return {
            tableKey: pending.tableKey,
            preferredSeat: /^[NESW]$/.test(pending.preferredSeat || "") ? pending.preferredSeat : null,
            spectate: pending.spectate === true,
          };
        }
      } catch (e) {
        /* localStorage unavailable or malformed — use the URL fallback */
      }
      if (/^[A-Za-z0-9]{4,20}$/.test(META.tableKey || "")) {
        return {
          tableKey: META.tableKey,
          preferredSeat: META.preferredSeat || null,
          spectate: META.spectate === true,
        };
      }
      return null;
    }

    function clearPendingJoin() {
      try {
        window.localStorage.removeItem(PENDING_JOIN_KEY);
      } catch (e) {
        /* localStorage unavailable — nothing to clear */
      }
    }

    let ws = null;
    let backoff = 500;
    let toastId = 0;
    let pendingJoinSent = false;
    let bidEffectTimer = null;
    let beloteEffectTimer = null;
    let joinEffectTimer = null;
    let redealEffectTimer = null;
    let bidAnnouncementTimer = null;
    // Consecutive reconnect attempts that never managed to open. On the
    // méta-client this *might* mean our session no longer exists server-side
    // (server rebooted, or the session was reaped) — but it can equally be a
    // transient network blip (a phone waking from sleep reconnects its radio
    // over several seconds, during which every WS open fails). Those two cases
    // look identical from the WS alone, so once we hit the threshold we don't
    // wipe anything blindly: we ask the server which it is (see
    // `verifyThenReconnect`). Abandoning a still-live session here is exactly
    // what stranded a player on the landing page while their old socket kept
    // their seat at the table.
    let failedOpens = 0;
    const MAX_FAILED_OPENS = 5;

    function scheduleReconnect() {
      // Raise the full-screen blocking overlay (replaces the old transient
      // toast): while the socket is down, taps/clicks are dropped anyway, so we
      // make that explicit rather than letting the player click into the void.
      reconnecting.value = true;
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 4000);
    }

    function abandonDeadSession() {
      try {
        window.localStorage.removeItem("coinche.metaSessionId");
      } catch (e) {
        /* ignore */
      }
      // Only the méta-client can recover via the landing page; the mono-session
      // overlay has nowhere to bounce to, so it just keeps retrying.
      if (META.sessionId) window.location.replace("/");
    }

    // Reached after MAX_FAILED_OPENS consecutive WS opens failed. Before giving
    // up and wiping our session id, confirm with the server whether the session
    // is actually gone. Only a server-confirmed-dead session justifies bouncing
    // home; a live session (or an unreachable probe — i.e. the network itself is
    // down, not the session) keeps retrying so we land back in our seat.
    function verifyThenReconnect() {
      if (!META.sessionId) {
        // Mono-session overlay: no session concept, just keep retrying.
        failedOpens = 0;
        scheduleReconnect();
        return;
      }
      fetch("/api/session?id=" + encodeURIComponent(META.sessionId))
        .then((r) => (r.ok ? r.json() : { alive: false }))
        .then((data) => {
          if (data && data.alive) {
            failedOpens = 0; // still alive server-side — the WS drops are transient
            scheduleReconnect();
          } else {
            abandonDeadSession();
          }
        })
        .catch(() => {
          // Couldn't even reach the probe: the network is down, not the
          // session. Keep retrying rather than discarding a possibly-live seat.
          failedOpens = 0;
          scheduleReconnect();
        });
    }

    // -------- WebSocket (ConnectionLayer) --------
    function wsUrl() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const path = META.wsPath || "/ws";
      return `${proto}://${location.host}${path}`;
    }

    function connect() {
      let opened = false;
      ws = new WebSocket(wsUrl());
      ws.addEventListener("open", () => {
        opened = true;
        backoff = 500;
        failedOpens = 0; // a successful open means the session is alive
        reconnecting.value = false; // socket is live again — drop the overlay
        // Ask U2 to start streaming lobby updates so the join screen is live.
        sendAction("lobby", {});
      });
      ws.addEventListener("message", (event) => {
        let frame;
        try {
          frame = JSON.parse(event.data);
        } catch {
          return; // ignore unparseable frame
        }
        if (frame.type === "state") {
          applyState(frame.snapshot); // FULL replace — idempotent, no deltas
        } else if (frame.type === "error") {
          showToast(frame.message || frame.code || "Erreur", "error");
          bidSending.value = false; // an error reverts any pending affordance
          fillingBots.value = false;
        }
      });
      ws.addEventListener("close", () => {
        // Closed without ever opening this attempt: count it. A dead session
        // (server reboot / reaped) rejects the /s/<id>/ws upgrade, so the WS
        // closes without opening every time. But a phone waking from sleep also
        // fails to open for a few seconds while its radio reconnects — same
        // symptom, live session. After a few tries we therefore *ask* the
        // server which case it is instead of assuming the worst and wiping a
        // still-live session (which would strand the player on a fresh seat
        // while their old socket kept the real one).
        if (!opened) {
          failedOpens += 1;
          if (failedOpens >= MAX_FAILED_OPENS) {
            verifyThenReconnect();
            return;
          }
        }
        scheduleReconnect();
      });
      ws.addEventListener("error", () => {
        try {
          ws.close();
        } catch {
          /* the close handler drives the retry */
        }
      });
    }

    function sendAction(action, payload) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ action, ...(payload || {}) }));
    }

    function tryPendingJoin(snap) {
      if (!snap || !snap.flags) return;
      if (snap.flags.turn_timed_out) {
        // The session was expelled from its seat. A deep-link URL may still
        // name that table after a page refresh, but it must not silently take
        // over the bot that replaced this player.
        clearPendingJoin();
        pendingJoinSent = true;
        return;
      }
      if (snap.flags.joined_once) {
        clearPendingJoin();
        return;
      }
      if (pendingJoinSent) return;
      const pending = readPendingJoin();
      const name = lobby.name.trim();
      if (!pending || !name) return;

      pendingJoinSent = true;
      clearPendingJoin();
      const payload = { table_key: pending.tableKey, player_name: name };
      if (pending.preferredSeat) payload.seat = pending.preferredSeat;
      if (pending.spectate) payload.spectate = true;
      sendAction("join", payload);
    }

    // -------- snapshot ingestion + animation triggers --------
    function applyState(snap) {
      const prev = snapshot.value;

      // Deal animation: the local hand grew (a fresh deal), not a card played.
      if (
        prev &&
        snap.hand &&
        snap.hand.length > (prev.hand ? prev.hand.length : 0) &&
        !REDUCED_MOTION
      ) {
        dealing.value = true;
        setTimeout(() => (dealing.value = false), 900);
      }

      // Coinche flash: the coinche multiplier rose.
      if (
        prev &&
        snap.coinche_level > (prev.coinche_level || 1) &&
        !REDUCED_MOTION
      ) {
        badgeFlash.value = true;
        setTimeout(() => (badgeFlash.value = false), 700);
      }

      // Mirror the terminal's prominent Coinche/Surcoinche announcement as
      // soon as the server confirms the bid, before the auction settles.
      if (prev && snap.bid_effect_level > (prev.bid_effect_level || 1)) {
        bidEffect.value = snap.bid_effect_level;
        bidEffectKey.value += 1;
        if (bidEffectTimer) clearTimeout(bidEffectTimer);
        bidEffectTimer = setTimeout(() => {
          bidEffect.value = null;
          bidEffectTimer = null;
        }, 2400);
      }

      // Mirror the Coinche effect for a Belote/Rebelote declaration: the seq
      // counter bumps once per declaration, re-triggering the pop each time
      // (Belote first, Rebelote later in the same round).
      if (prev && snap.belote_effect_seq > (prev.belote_effect_seq || 0)) {
        beloteEffect.value = snap.belote_effect;
        beloteEffectKey.value += 1;
        if (beloteEffectTimer) clearTimeout(beloteEffectTimer);
        beloteEffectTimer = setTimeout(() => {
          beloteEffect.value = null;
          beloteEffectTimer = null;
        }, 2400);
      }

      // Join effect: a human player joined the table (empty seat, bot
      // replacement, or reconnection). The seq counter bumps once per join,
      // re-triggering the pop each time.
      if (prev && snap.join_effect_seq > (prev.join_effect_seq || 0)) {
        joinEffect.value = snap.join_effect_name;
        joinEffectKey.value += 1;
        if (joinEffectTimer) clearTimeout(joinEffectTimer);
        joinEffectTimer = setTimeout(() => {
          joinEffect.value = null;
          joinEffectTimer = null;
        }, 2400);
      }

      // Redeal effect: all players passed and the deck is being reshuffled.
      if (prev && snap.redeal_effect_seq > (prev.redeal_effect_seq || 0)) {
        redealEffect.value = true;
        redealEffectKey.value += 1;
        if (redealEffectTimer) clearTimeout(redealEffectTimer);
        redealEffectTimer = setTimeout(() => {
          redealEffect.value = false;
          redealEffectTimer = null;
        }, 3000);
      }

      // A contract bid is a server-confirmed announcement. Compare the
      // authoritative standing bid rather than local clicks so bot and remote
      // player announcements receive the same feedback.
      const previousBid = (prev && prev.current_bid) || {};
      const currentBid = snap.current_bid || {};
      if (
        prev &&
        currentBid.trump &&
        (currentBid.trump !== previousBid.trump ||
          currentBid.points !== previousBid.points ||
          currentBid.seat !== previousBid.seat)
      ) {
        const points =
          currentBid.points === "capot" ? "Capot" : currentBid.points;
        bidAnnouncement.value = {
          name: (snap.players || {})[currentBid.seat] || currentBid.seat,
          label: `${points} ${currentBid.trump}`,
        };
        bidAnnouncementKey.value += 1;
        if (bidAnnouncementTimer) clearTimeout(bidAnnouncementTimer);
        bidAnnouncementTimer = setTimeout(() => {
          bidAnnouncement.value = null;
          bidAnnouncementTimer = null;
        }, 2200);
      }

      // Trick sweep (two-stage flip): when the just-completed 4-card trick
      // clears, sweep the cards toward the winner. The winner is whose_turn at
      // the moment the trick was full (TRICK_RESULT), captured from prev.
      if (prev && !REDUCED_MOTION) {
        const prevTrick = Object.keys(prev.current_trick || {}).length;
        const nowTrick = Object.keys(snap.current_trick || {}).length;
        if (prevTrick === 4 && nowTrick === 0 && prev.whose_turn) {
          const dir = visualPosition(prev.whose_turn, snap.seat || prev.seat || "S");
          sweepClass.value = "sweep-" + dir;
          setTimeout(() => (sweepClass.value = null), 450);
        }
      }

      // Confetti on game over.
      if (
        snap.flags &&
        snap.flags.game_over &&
        !(prev && prev.flags && prev.flags.game_over)
      ) {
        spawnConfetti();
      }

      // Chat unread badge when the panel is closed.
      if (
        prev &&
        snap.chat_messages &&
        snap.chat_messages.length > (prev.chat_messages || []).length
      ) {
        if (!chatOpen.value)
          unread.value += snap.chat_messages.length - prev.chat_messages.length;
      }

      // A new snapshot confirms any in-flight bid.
      bidSending.value = false;
      fillingBots.value = false;

      // Surface server errors as toast (they arrive via last_error).
      if (
        snap.last_error &&
        snap.last_error !== (prev && prev.last_error)
      ) {
        showToast(snap.last_error, "error");
      }

      if (
        snap.turn_timeout_message &&
        snap.turn_timeout_message !== (prev && prev.turn_timeout_message)
      ) {
        showToast(snap.turn_timeout_message, "error", 8000);
      }

      // Back in the lobby (the server sent LEFT and cleared joined_once): a
      // pending leave has completed, so drop the "Départ en cours…" state.
      if (leaving.value && !(snap.flags && snap.flags.joined_once)) {
        leaving.value = false;
        if (leavingTimer) {
          clearTimeout(leavingTimer);
          leavingTimer = null;
        }
      }

      snapshot.value = snap; // full replace
      tryPendingJoin(snap);
    }

    function spawnConfetti() {
      if (REDUCED_MOTION) return;
      const colors = ["#d4af37", "#e8cf7a", "#26c6da", "#c94fd8", "#f5f5f5"];
      const pieces = [];
      for (let i = 0; i < 90; i++) {
        pieces.push({
          left: Math.random() * 100,
          delay: Math.random() * 1.5,
          dur: 2.5 + Math.random() * 2,
          color: colors[i % colors.length],
          rot: Math.random() * 360,
        });
      }
      confetti.value = pieces;
      setTimeout(() => (confetti.value = []), 6000);
    }

    function showToast(message, type = "error", ttl = 5000) {
      const id = ++toastId;
      if (type === "error") {
        toasts.value = toasts.value.filter((t) => t.type !== "error");
      }
      toasts.value.push({ id, message, type });
      setTimeout(() => {
        toasts.value = toasts.value.filter((t) => t.id !== id);
      }, ttl);
      return id;
    }
    function dismissToast(id) {
      toasts.value = toasts.value.filter((t) => t.id !== id);
    }

    // -------- derived view state --------
    // "In a table view" is keyed off joined_once (a spectator holds no seat but
    // is very much on the table), not off holding a seat.
    const joined = computed(
      () => snapshot.value && snapshot.value.flags && snapshot.value.flags.joined_once,
    );
    const isSpectator = computed(
      () => !!(snapshot.value && snapshot.value.is_spectator),
    );
    const flags = computed(
      () => (snapshot.value && snapshot.value.flags) || {},
    );
    // The seat the board is oriented around. A seated player sits at the
    // bottom (south); a spectator has no seat, so the board is shown from a
    // fixed "South = N/S team" viewpoint so N/E/S/W still render in place.
    const viewSeat = computed(() => {
      const s = snapshot.value;
      return (s && s.seat) || "S";
    });
    const localTeam = computed(() => {
      const s = snapshot.value;
      return s && s.seat && s.team_of ? s.team_of[s.seat] : "NS";
    });
    const otherTeam = computed(() => (localTeam.value === "NS" ? "EW" : "NS"));

    function teamLabel(teamId, fallback) {
      const names = (snapshot.value && snapshot.value.team_names) || {};
      return names[teamId] || fallback;
    }
    const nousLabel = computed(() => teamLabel(localTeam.value, "Nous"));
    const euxLabel = computed(() => teamLabel(otherTeam.value, "Eux"));
    const teamPlayers = computed(() => {
      const s = snapshot.value || {};
      const players = s.players || {};
      const teamOf = s.team_of || {};
      const namesFor = (team) =>
        Object.keys(players)
          .filter((seatId) => teamOf[seatId] === team)
          .map((seatId) => players[seatId])
          .join(" & ");
      return {
        nous: namesFor(localTeam.value),
        eux: namesFor(otherTeam.value),
      };
    });
    const nousScore = computed(() => {
      const sc = (snapshot.value && snapshot.value.cumulative_scores) || {};
      return sc[localTeam.value] || 0;
    });
    const euxScore = computed(() => {
      const sc = (snapshot.value && snapshot.value.cumulative_scores) || {};
      return sc[otherTeam.value] || 0;
    });

    // Seats arranged into visual slots (local = south), with all per-seat data.
    const seats = computed(() => {
      const s = snapshot.value;
      if (!s || !s.players) return [];
      const players = s.players || {};
      const teamOf = s.team_of || {};
      const trick = s.current_trick || {};
      const marks = s.bid_marks || {};
      const conn = s.connection_status || {};
      const bots = s.bots || {};
      const botTypes = s.bot_types || {};
      return Object.keys(players).map((seatId) => {
        return {
          seatId,
          slot: visualPosition(seatId, viewSeat.value),
          name: players[seatId],
          teamClass: teamOf[seatId] === localTeam.value ? "nous" : "eux",
          playedCard: trick[seatId] || null,
          bidMark: marks[seatId] || null,
          isTurn: s.whose_turn === seatId,
          isDealer: s.dealer_seat === seatId,
          isBot: bots[seatId] === true,
          botType: botTypes[seatId] || null,
          connected: conn[seatId] !== false,
        };
      });
    });

    // Cards currently on the table (for the converging trick animation).
    const trickCards = computed(() => {
      const s = snapshot.value;
      if (!s) return [];
      const trick = s.current_trick || {};
      return Object.keys(trick).map((seatId) => ({
        card: trick[seatId],
        slot: visualPosition(seatId, viewSeat.value),
      }));
    });

    // Last-trick corner (compact 3x3, mirrors ui.last_trick_grid).
    const lastTrickCells = computed(() => {
      const s = snapshot.value;
      if (!s || !s.last_trick || !Object.keys(s.last_trick).length) return null;
      const grid = Array(9).fill(null); // slots: 1=N,3=W,5=E,7=S in a 3x3
      const slotIndex = { north: 1, west: 3, east: 5, south: 7 };
      for (const seatId of Object.keys(s.last_trick)) {
        const slot = visualPosition(seatId, viewSeat.value);
        grid[slotIndex[slot]] = s.last_trick[seatId];
      }
      return grid;
    });

    const handCards = computed(() => {
      const s = snapshot.value;
      if (!s) return [];
      const pc = pendingCard.value;
      const bidding = !!s.pending_bid_request;
      return (s.hand || []).map((card) => ({
        card,
        legal: !bidding,
        illegal: false,
        pending: pc === card,
      }));
    });

    const trumpSuit = computed(() => {
      const s = snapshot.value;
      return s ? s.trump : null;
    });

    const contract = computed(() => {
      const s = snapshot.value;
      if (!s || !s.trump || s.contract_points == null) return null;
      const pts = s.contract_points === "capot" ? "Capot" : s.contract_points;
      let label = `Annonce : ${pts} ${s.trump}`;
      if (s.coinche_level > 1) label += ` x${s.coinche_level}`;
      // Attribution : qui a pris et pour quelle équipe.
      const bidderSeat = s.contract_bidder;
      if (bidderSeat) {
        const bidderName = (s.players || {})[bidderSeat] || bidderSeat;
        const teamId = (s.team_of || {})[bidderSeat];
        const teamName = teamId ? teamLabel(teamId, teamId) : null;
        label += teamName
          ? ` — ${bidderName} (${teamName})`
          : ` — ${bidderName}`;
      }
      return label;
    });

    const currentBid = computed(() => {
      const s = snapshot.value;
      if (!s || contract.value) return null; // once settled, the badge shows instead
      const b = s.current_bid;
      if (!b || !b.trump) return null;
      const pts = b.points === "capot" ? "Capot" : b.points;
      return `${pts} ${b.trump}`;
    });

    const bidRequest = computed(() =>
      snapshot.value ? snapshot.value.pending_bid_request : null,
    );
    const canFillBots = computed(
      () => !!(snapshot.value && snapshot.value.can_fill_bots),
    );

    const turnText = computed(() => {
      const s = snapshot.value;
      if (!s || !s.whose_turn) return "";
      if (s.whose_turn === s.seat) return "À vous de jouer";
      const who = (s.players || {})[s.whose_turn] || s.whose_turn;
      return "Au tour de " + who;
    });
    const turnSeconds = computed(() => {
      const deadline = snapshot.value && snapshot.value.turn_deadline;
      if (!deadline) return null;
      return Math.max(0, Math.ceil(deadline - countdownNow.value));
    });
    const turnCountdown = computed(() => {
      if (turnSeconds.value == null) return "";
      return `${Math.floor(turnSeconds.value / 60)}:${String(turnSeconds.value % 60).padStart(2, "0")}`;
    });

    // Round recap detail (uses last_round_contract — verified present in U1's
    // snapshot_to_dict).
    const recapContract = computed(() => {
      const s = snapshot.value;
      const c = s && s.last_round_contract;
      if (!c) return null;
      const pts = c.points === "capot" ? "Capot" : c.points;
      // c.result is the per-team `contract_result` string from rules.score_round.
      const honored = c.result === "made" || c.result === "capot_achieved";
      return { label: `${pts} ${c.trump}`, honored };
    });

    const roundScores = computed(() => {
      const s = snapshot.value;
      const rs = s && s.last_round_score;
      if (!rs) return null;
      // Per-team results distinguish points captured in tricks from the score
      // awarded for the round (which can include a contract or belote bonus).
      const scoreFor = (team) => {
        const t = rs[team];
        if (t == null) return { cardPoints: 0, beloteBonus: 0, total: 0 };
        if (typeof t !== "object") return { cardPoints: 0, beloteBonus: 0, total: t };
        return {
          cardPoints: t.card_points ?? 0,
          beloteBonus: t.belote_bonus ?? 0,
          total: t.total ?? 0,
        };
      };
      return {
        nous: scoreFor(localTeam.value),
        eux: scoreFor(otherTeam.value),
      };
    });

    const roundOutcome = computed(() => {
      const scores = roundScores.value;
      if (!scores) return null;
      if (scores.nous.total > scores.eux.total) return "won";
      if (scores.nous.total < scores.eux.total) return "lost";
      return "tied";
    });

    const winnerLabel = computed(() => {
      const s = snapshot.value;
      if (!s || !s.winning_team) return "";
      const won =
        s.winning_team === localTeam.value ? nousLabel.value : euxLabel.value;
      return won;
    });
    const finalNous = computed(() => {
      const s = snapshot.value;
      return s && s.final_scores ? s.final_scores[localTeam.value] || 0 : 0;
    });
    const finalEux = computed(() => {
      const s = snapshot.value;
      return s && s.final_scores ? s.final_scores[otherTeam.value] || 0 : 0;
    });

    const statusMessage = computed(() =>
      snapshot.value
        ? snapshot.value.last_action || snapshot.value.status_message
        : "",
    );

    // -------- actions --------
    function playCard(card) {
      const s = snapshot.value;
      if (!s) return;
      const hasPlayedThisTrick = Object.prototype.hasOwnProperty.call(s.current_trick || {}, s.seat);
      const isOurTurn = s.whose_turn === s.seat && !hasPlayedThisTrick;
      if (isOurTurn) {
        // Our turn: play immediately (server-authoritative validation).
        pendingCard.value = null;
        sendAction("play", { card });
      } else if (s.hand && s.hand.includes(card)) {
        // Not our turn: toggle queue.  Clicking the same card again cancels.
        if (pendingCard.value === card) {
          pendingCard.value = null;
          document.activeElement?.blur();
        } else {
          pendingCard.value = card;
        }
      }
    }
    function submitBid(payload) {
      bidSending.value = true;
      sendAction("bid", payload);
    }
    function sendChat(text) {
      sendAction("chat", { text });
    }
    function doRematch() {
      sendAction("rematch", {});
    }
    function continueRound() {
      sendAction("continue", {});
    }
    function fillBots() {
      fillingBots.value = true;
      sendAction("fill_bots", {});
    }
    function changeBotType(seatId) {
      const s = snapshot.value;
      const types = (s && s.available_bot_types) || [];
      if (!types.length) return;
      const current = (s.bot_types || {})[seatId] || "smart";
      const next = types[(types.indexOf(current) + 1) % types.length];
      sendAction("set_bot_type", { seat: seatId, bot_type: next });
    }
    function leaveTable() {
      // Available before AND during a game. Pre-game the seat is freed; mid-game
      // the server hands the seat to a bot so the other players can finish, so
      // ask for confirmation first. Either way the snapshot flips back to the
      // lobby on the LEFT the server sends.
      //
      // Already leaving: the server got our "leave" and just hasn't flipped us
      // back to the lobby yet (mid-game it finishes the in-flight trick/bot
      // turns first, which can take a few seconds). Swallow extra clicks so
      // they don't read as "nothing happened" and pile up.
      if (leaving.value) return;

      // Mid-game confirmation is inline (not window.confirm): the first click
      // arms the button (it turns red and reads "Confirmer"), the second click
      // within a few seconds actually leaves. The armed state auto-resets so a
      // stray first click doesn't leave the button stuck.
      const midGame = !isSpectator.value && !canFillBots.value;
      if (midGame && !leaveArmed.value) {
        leaveArmed.value = true;
        if (leaveDisarmTimer) clearTimeout(leaveDisarmTimer);
        leaveDisarmTimer = setTimeout(() => {
          leaveArmed.value = false;
          leaveDisarmTimer = null;
        }, 4000);
        return;
      }
      if (leaveDisarmTimer) {
        clearTimeout(leaveDisarmTimer);
        leaveDisarmTimer = null;
      }
      leaveArmed.value = false;
      // Show a pending state until the server returns us to the lobby. Mid-game
      // the seat is handed to a bot only once the current trick/bot turns
      // finish, so the switch isn't instant — without this the button looks
      // dead and the player keeps clicking. A safety timeout clears it in case
      // the LEFT snapshot never arrives (e.g. a dropped socket), so the button
      // can never get stuck disabled.
      leaving.value = true;
      if (leavingTimer) clearTimeout(leavingTimer);
      leavingTimer = setTimeout(() => {
        leaving.value = false;
        leavingTimer = null;
      }, 15000);
      sendAction("leave", {});
    }
    function joinTable() {
      if (!lobby.name.trim() || !lobby.table.trim()) return;
      rememberName(lobby.name);
      const payload = {
        table_key: lobby.table.trim(),
        player_name: lobby.name.trim(),
      };
      if (lobby.team.trim()) payload.team_name = lobby.team.trim();
      sendAction("join", payload);
    }
    function disconnect() {
      try {
        window.localStorage.clear();
      } catch (e) {
        /* localStorage unavailable — navigation still returns to the landing page */
      }
      window.location.replace("/");
    }
    function toggleChat() {
      chatOpen.value = !chatOpen.value;
      if (chatOpen.value) unread.value = 0;
    }

    // Lobby occupants grouped by team (rendered from players/team_of).
    const lobbyTeams = computed(() => {
      const s = snapshot.value;
      const players = (s && s.players) || {};
      const teamOf = (s && s.team_of) || {};
      const group = (team) =>
        Object.keys(players)
          .filter((seatId) => teamOf[seatId] === team)
          .map((seatId) => players[seatId]);
      return {
        nsLabel: teamLabel("NS", "Équipe 1"),
        ewLabel: teamLabel("EW", "Équipe 2"),
        ns: group("NS"),
        ew: group("EW"),
      };
    });

    // --- Web lobby: every live table as a mini-table ---------------------
    // Which side (NS / EW) a seat belongs to — mirrors game.TEAM_OF so the
    // mini-tables place players on the same two teams as the game.
    const SEAT_TEAM = { N: "NS", S: "NS", E: "EW", W: "EW" };
    // Fixed seat slots per team so an empty chair still renders in place.
    const TEAM_SEATS = { NS: ["N", "S"], EW: ["E", "W"] };
    // Fake face-down cards shown on a table that's playing / full, purely
    // decorative (never real cards — those only ever belong to your own seat).
    const FAKE_HAND = [0, 1, 2];

    const lobbyTables = computed(() => {
      const s = snapshot.value;
      const raw = (s && s.tables) || [];
      return raw
        .map((t) => {
          const bySeat = {};
          for (const p of t.players || []) bySeat[p.seat] = p;
          const teamOf = (team) =>
            TEAM_SEATS[team].map((seatId) => {
              const p = bySeat[seatId];
              const bot = !!(p && p.is_bot);
              return {
                seat: seatId,
                name: p ? p.name : "",
                empty: !p,
                bot,
                offline: !!(p && p.connected === false),
                // A bot chair on a running table can be taken over by a human
                // (the server's replace-a-bot path); the seat click joins there.
                replaceable: bot && !!t.in_progress,
              };
            });
          const filled = t.seats_filled || 0;
          const full = filled >= 4;
          // A table with bots is one a human can sit down at mid-game by taking
          // over a bot's chair, so it's joinable even while "en cours"/full.
          const hasBots = !!t.in_progress && (t.players || []).some((p) => p.is_bot);
          let status = "waiting";
          if (t.in_progress) status = "playing";
          else if (full) status = "full";
          return {
            key: t.table_key,
            inProgress: !!t.in_progress,
            filled,
            full,
            status,
            hasBots,
            spectators: t.spectators || 0,
            // A table is joinable from the web when it hasn't started and still
            // has a free seat, OR it's running but has a bot to replace; the
            // server remains the authority.
            joinable: (!t.in_progress && !full) || hasBots,
            // Any table can be watched; the "Regarder" affordance is offered
            // wherever sitting down isn't possible (full or already playing).
            spectatable: t.in_progress || full,
            ns: teamOf("NS"),
            ew: teamOf("EW"),
          };
        })
        .sort((a, b) => a.key.localeCompare(b.key));
    });

    // The lobby only knows its tables once the first snapshot lands. Until then
    // we show a loading state rather than the (misleading) "no tables" message,
    // so a slow WS connection doesn't flash "aucune table" before the tables pop
    // in. `snapshot` is null before the first {type:"state"} frame.
    const lobbyLoaded = computed(() => snapshot.value !== null);
    const availableBotTypes = computed(() => {
      const types = snapshot.value && snapshot.value.available_bot_types;
      return Array.isArray(types) && types.length ? types : ["smart"];
    });
    const tableOptionsOpen = ref(false);
    const tableOptions = reactive({
      name: "",
      suppressDiscordNotification: false,
      botType: "smart",
      coincheBlocksBidding: true,
    });
    const discordNotificationsEnabled = computed({
      get: () => !tableOptions.suppressDiscordNotification,
      set: (enabled) => {
        tableOptions.suppressDiscordNotification = !enabled;
      },
    });
    const tableNameEdited = ref(false);

    // Choose a random venue key once, then preserve it in the editable field.
    // Re-roll only when another table takes the untouched suggested key.
    const nextTableKey = ref("table1");
    function suggestedTableKey() {
      const keys = new Set(lobbyTables.value.map((t) => t.key.toLowerCase()));
      if (!tableNames.length) return "table1";
      const base = tableNames[Math.floor(Math.random() * tableNames.length)];
      let key = base;
      let suffix = 2;
      while (keys.has(key.toLowerCase())) {
        const suffixText = String(suffix);
        key = base.slice(0, TABLE_KEY_MAX_LENGTH - suffixText.length) + suffixText;
        suffix += 1;
      }
      return key;
    }

    function refreshSuggestedTableKey() {
      const keys = new Set(lobbyTables.value.map((t) => t.key.toLowerCase()));
      if (tableNameEdited.value || (tableOptions.name && !keys.has(tableOptions.name.toLowerCase()))) return;
      nextTableKey.value = suggestedTableKey();
      tableOptions.name = nextTableKey.value;
    }

    watch(lobbyTables, refreshSuggestedTableKey, { immediate: true });

    function joinSpecificTable(tableKey, teamLabelText, seat, tableSettings = null) {
      const name = lobby.name.trim();
      if (!name) {
        showToast("Entrez votre nom d'abord", "error");
        return;
      }
      rememberName(name);
      const payload = { table_key: tableKey, player_name: name };
      if (teamLabelText) payload.team_name = teamLabelText;
      // When a specific empty chair is clicked, ask the server for that exact
      // seat (the server stays authoritative and falls back if it's taken).
      if (seat) payload.seat = seat;
      if (tableSettings) {
        if (tableSettings.suppressDiscordNotification) payload.suppress_discord_notification = true;
        if (!tableSettings.coincheBlocksBidding) payload.coinche_blocks_bidding = false;
        if (tableSettings.botType !== "smart") payload.bot_type = tableSettings.botType;
      }
      sendAction("join", payload);
    }

    // Watch a table as a seatless spectator — allowed even when it's full or a
    // game is already under way (that's exactly what you can't sit at). The
    // server assigns a display name and streams the public board + chat.
    function spectateTable(tableKey) {
      const name = lobby.name.trim();
      if (!name) {
        showToast("Entrez votre nom d'abord", "error");
        return;
      }
      rememberName(name);
      sendAction("join", { table_key: tableKey, player_name: name, spectate: true });
    }

    function createTable() {
      const tableKey = tableOptions.name.trim() || nextTableKey.value;
      if (!/^[A-Za-z0-9]{4,20}$/.test(tableKey)) {
        showToast("Le nom doit contenir de 4 à 20 lettres ou chiffres", "error");
        return;
      }
      joinSpecificTable(tableKey, lobbyTeams.value.nsLabel, null, tableOptions);
    }

    watch(chatOpen, (open) => {
      if (open) unread.value = 0;
    });

    watch(
      () => snapshot.value && snapshot.value.turn_deadline,
      (deadline) => {
        if (countdownInterval) {
          clearInterval(countdownInterval);
          countdownInterval = null;
        }
        countdownWarnings = new Set();
        if (!deadline) return;
        const tick = () => {
          countdownNow.value = Date.now() / 1000;
          const remaining = Math.max(0, Math.ceil(deadline - countdownNow.value));
          for (const threshold of [60, 30]) {
            const key = `${deadline}:${threshold}`;
            if (remaining <= threshold && remaining > 0 && !countdownWarnings.has(key)) {
              countdownWarnings.add(key);
              showToast(`Il reste ${remaining} secondes pour jouer.`, "info", 4000);
            }
          }
          if (remaining === 0 && countdownInterval) {
            clearInterval(countdownInterval);
            countdownInterval = null;
          }
        };
        tick();
        countdownInterval = setInterval(tick, 1000);
      },
    );

    // Auto-play: when our turn arrives and a card was pre-selected, play it.
    // A completed trick still names its winner as `whose_turn` during the
    // visual pause, so wait for TRICK_CLEARED to empty the table first.
    watch(
      () => {
        const s = snapshot.value;
        return [s ? s.whose_turn : null, s ? Object.keys(s.current_trick || {}).length : 0];
      },
      ([turn, trickSize]) => {
        if (!pendingCard.value) return;
        const s = snapshot.value;
        if (!s || turn !== s.seat || trickSize === 4) return;
        const card = pendingCard.value;
        pendingCard.value = null;
        sendAction("play", { card });
      },
    );

    // Clear pending card when the hand changes (new deal / resync) and it is
    // no longer present.
    watch(
      () => snapshot.value && snapshot.value.hand,
      (hand) => {
        if (pendingCard.value && hand && !hand.includes(pendingCard.value)) {
          pendingCard.value = null;
        }
      },
    );

    onMounted(connect);
    onUnmounted(() => {
      if (countdownInterval) clearInterval(countdownInterval);
    });

    return {
      snapshot,
      toasts,
      chatOpen,
      chatDraft,
      unread,
      bidSending,
      fillingBots,
      leaveArmed,
      leaving,
      shakeCard,
      dealing,
      badgeFlash,
      bidEffect,
      bidEffectKey,
      beloteEffect,
      beloteEffectKey,
      redealEffect,
      redealEffectKey,
      bidAnnouncement,
      bidAnnouncementKey,
      sweepClass,
      confetti,
      reconnecting,
      lobby,
      isMetaClient,
      REDUCED_MOTION,
      joined,
      isSpectator,
      flags,
      localTeam,
      nousLabel,
      euxLabel,
      teamPlayers,
      nousScore,
      euxScore,
      seats,
      trickCards,
      lastTrickCells,
      handCards,
      trumpSuit,
      contract,
      currentBid,
      bidRequest,
      canFillBots,
      turnText,
      turnSeconds,
      turnCountdown,
      recapContract,
      roundScores,
      roundOutcome,
      winnerLabel,
      finalNous,
      finalEux,
      statusMessage,
      lobbyTeams,
      lobbyTables,
      lobbyLoaded,
      availableBotTypes,
      tableOptionsOpen,
      tableOptions,
      discordNotificationsEnabled,
      tableNameEdited,
      nextTableKey,
      joinSpecificTable,
      showToast,
      dismissToast,
      spectateTable,
      createTable,
      playCard,
      submitBid,
      sendChat,
      doRematch,
      continueRound,
      fillBots,
      changeBotType,
      leaveTable,
      joinTable,
      disconnect,
      toggleChat,
    };
  },
  template: `
    <!-- Toasts (transient errors / reconnection notice) -->
    <div class="toast-stack" aria-live="assertive">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="{ 'toast--info': t.type === 'info', 'toast--error': t.type === 'error' }"
           @click="t.type === 'error' && dismissToast(t.id)">{{ t.message }}</div>
    </div>

    <!-- Reconnection overlay: full-screen, semi-transparent, and it swallows
         pointer events so a returning (backgrounded) tab can't fire actions
         into a dead socket. Shown whenever the WS isn't open. -->
    <div v-if="reconnecting" class="reconnect-overlay" role="alertdialog" aria-live="assertive"
         aria-label="Reconnexion en cours" data-testid="reconnect-overlay">
      <span class="lobby__spinner" aria-hidden="true"></span>
      <p class="reconnect-overlay__label">Reconnexion…</p>
    </div>

    <!-- Coinche / Surcoinche confirmation from the server-authoritative bid state. -->
    <div v-if="bidEffect" :key="bidEffectKey" class="bid-effect" :class="{ 'bid-effect--surcoinche': bidEffect >= 4 }"
         role="status" aria-live="assertive">
      <span v-if="bidEffect >= 4">🔥 SURCOINCHE ! ×4 🔥</span>
      <span v-else>⚡ COINCHE ! ×2 ⚡</span>
    </div>
    <!-- Belote / Rebelote declaration, mirroring the Coinche effect. -->
    <div v-if="beloteEffect" :key="'belote-' + beloteEffectKey" class="bid-effect bid-effect--belote"
         role="status" aria-live="assertive">
      <span v-if="beloteEffect === 'rebelote'">👑 REBELOTE !</span>
      <span v-else>💑 BELOTE !</span>
    </div>
    <!-- Join effect: a human player arrived at the table. -->
    <div v-if="joinEffect" :key="'join-' + joinEffectKey" class="bid-effect bid-effect--join"
         role="status" aria-live="assertive">
      <span>🙋 {{ joinEffect }} a rejoint !</span>
    </div>
    <!-- Redeal: all players passed, reshuffling the deck. -->
    <div v-if="redealEffect" :key="'redeal-' + redealEffectKey" class="bid-effect bid-effect--redeal"
         role="status" aria-live="assertive">
      <span>🔀 Nouvelle donne — on redistribue !</span>
    </div>
    <div v-if="bidAnnouncement" :key="bidAnnouncementKey" class="bid-effect bid-effect--announcement"
         role="status" aria-live="polite">
      <span class="bid-effect__actor">{{ bidAnnouncement.name }} annonce</span>
      <strong class="bid-effect__value">{{ bidAnnouncement.label }}</strong>
    </div>

    <!-- ================= LOBBY (not joined) ================= -->
    <div v-if="!joined" class="lobby">
      <div class="lobby__inner">
        <header class="lobby__header">
          <h1 class="lobby__title"><img src="favicon.ico" alt="" class="lobby__favicon" /> Coinche CLI Online</h1>
          <div class="lobby__namefield">
            <label for="lobby-name">Votre nom</label>
            <input id="lobby-name" type="text" v-model="lobby.name" maxlength="24"
                   data-testid="lobby-name" placeholder="Aline" />
          </div>
          <button class="leave-btn" type="button" data-testid="lobby-disconnect" @click="disconnect">
            Déconnexion
          </button>
          <a v-if="isMetaClient" class="pairing-badge" href="/pair">
            <span class="pairing-badge__label">Acces rapide</span>
          </a>
        </header>

        <!-- Before the first snapshot lands the table list is unknown; show a
             loading state instead of "aucune table" so a slow WS connection
             doesn't flash an empty lobby before the tables appear. -->
        <div v-if="!lobbyLoaded" class="lobby__loading" data-testid="lobby-loading" role="status" aria-live="polite">
          <span class="lobby__spinner" aria-hidden="true"></span>
          <span>Chargement des tables…</span>
        </div>

        <div v-else class="lobby__grid">
          <!-- Create-a-table card -->
          <div class="minitable minitable--create">
            <button class="minitable__create-button" type="button" data-testid="lobby-create" @click="createTable()">
              <div class="minitable--create__plus">＋</div>
              <div class="minitable--create__label">Créer table</div>
              <div class="minitable--create__hint">{{ tableOptions.name || nextTableKey }}</div>
            </button>
            <button class="minitable__options-button" type="button" data-testid="table-options"
                    :aria-expanded="tableOptionsOpen" @click="tableOptionsOpen = !tableOptionsOpen">Options de table</button>
            <div v-if="tableOptionsOpen" class="table-options" data-testid="table-options-panel">
              <label>Nom de la table
                <input v-model="tableOptions.name" maxlength="20" @input="tableNameEdited = true" />
              </label>
              <label>Type de bot
                <select v-model="tableOptions.botType">
                  <option v-for="botType in availableBotTypes" :key="botType" :value="botType">
                    {{ botType === "smart" ? "Smart" : botType === "maestro" ? "Maestro - audacieux" : botType === "cloclo" ? "Cloclo - IA offensive" : botType }}
                  </option>
                </select>
              </label>
              <div class="table-options__setting">
                <span>Coincher bloque les annonces</span>
                <button class="table-options__chip" type="button"
                        :class="{ 'table-options__chip--active': tableOptions.coincheBlocksBidding }"
                        :aria-pressed="tableOptions.coincheBlocksBidding"
                        @click="tableOptions.coincheBlocksBidding = !tableOptions.coincheBlocksBidding">
                  {{ tableOptions.coincheBlocksBidding ? 'Oui' : 'Non' }}
                </button>
              </div>
              <div class="table-options__setting">
                <span>Notification Discord</span>
                <button class="table-options__chip" type="button"
                        :class="{ 'table-options__chip--active': discordNotificationsEnabled }"
                        :aria-pressed="discordNotificationsEnabled"
                        data-testid="suppress-discord-notification"
                        @click="discordNotificationsEnabled = !discordNotificationsEnabled">
                  {{ discordNotificationsEnabled ? 'Activée' : 'Désactivée' }}
                </button>
              </div>
            </div>
          </div>

          <!-- One mini-table per live table -->
          <div v-for="t in lobbyTables" :key="t.key" class="minitable"
               :class="{ 'minitable--playing': t.status === 'playing', 'minitable--full': t.status === 'full', 'minitable--waiting': t.status === 'waiting' }"
               :data-testid="'minitable-' + t.key">
            <div class="minitable__head">
              <span class="minitable__key">{{ t.key }}</span>
              <span class="minitable__badge" :class="'minitable__badge--' + t.status">
                <template v-if="t.status === 'playing'">🎴 En cours</template>
                <template v-else-if="t.status === 'full'">✔ Complète</template>
                <template v-else>⏳ En attente ({{ t.filled }}/4)</template>
              </span>
            </div>

            <div class="minitable__felt">
              <!-- fake face-down cards in the middle when the game is live/full -->
              <div v-if="t.status !== 'waiting'" class="minitable__cards" aria-hidden="true">
                <span v-for="c in [0,1,2]" :key="c" class="fakecard"></span>
              </div>
              <div v-else class="minitable__await" aria-hidden="true">En attente de joueurs…</div>

              <!-- North / South = Équipe 1 ; West / East = Équipe 2 -->
              <div v-for="p in t.ns" :key="'ns'+p.seat" class="seatchip" :class="'seatchip--' + (p.seat === 'N' ? 'north' : 'south')">
                <span v-if="p.empty" class="seatchip--empty"
                      @click="t.joinable && joinSpecificTable(t.key, lobbyTeams.nsLabel, p.seat)"
                      :class="{ 'seatchip--joinable': t.joinable }">＋ libre</span>
                <span v-else class="seatchip__name" :class="{ 'seatchip__name--replaceable': p.replaceable }"
                      :title="p.replaceable ? 'Remplacer ce bot' : ''"
                      @click="p.replaceable && joinSpecificTable(t.key, lobbyTeams.nsLabel, p.seat)">{{ p.name }}<span v-if="p.bot" class="seatchip__tag">bot</span><span v-if="p.offline" class="seatchip__tag seatchip__tag--off">hors-ligne</span></span>
              </div>
              <div v-for="p in t.ew" :key="'ew'+p.seat" class="seatchip" :class="'seatchip--' + (p.seat === 'W' ? 'west' : 'east')">
                <span v-if="p.empty" class="seatchip--empty"
                      @click="t.joinable && joinSpecificTable(t.key, lobbyTeams.ewLabel, p.seat)"
                      :class="{ 'seatchip--joinable': t.joinable }">＋ libre</span>
                <span v-else class="seatchip__name" :class="{ 'seatchip__name--replaceable': p.replaceable }"
                      :title="p.replaceable ? 'Remplacer ce bot' : ''"
                      @click="p.replaceable && joinSpecificTable(t.key, lobbyTeams.ewLabel, p.seat)">{{ p.name }}<span v-if="p.bot" class="seatchip__tag">bot</span><span v-if="p.offline" class="seatchip__tag seatchip__tag--off">hors-ligne</span></span>
              </div>
            </div>

            <div class="minitable__foot">
              <button v-if="t.joinable" class="minitable__join" :data-testid="'join-' + t.key"
                      @click="t.hasBots ? showToast('Choisissez votre place', 'info', 3500) : joinSpecificTable(t.key, '')">{{ t.hasBots ? '🤖 Remplacer un bot' : 'Rejoindre' }}</button>
              <button v-if="t.hasBots || !t.joinable" class="minitable__spectate" :data-testid="'spectate-' + t.key"
                      @click="spectateTable(t.key)">👁 Regarder</button>
            </div>
            <div v-if="t.spectators" class="minitable__spectators" :data-testid="'spectators-' + t.key">
              👁 {{ t.spectators }} spectateur{{ t.spectators > 1 ? 's' : '' }}
            </div>
          </div>
        </div>

        <p v-if="lobbyLoaded && !lobbyTables.length" class="lobby__empty">
          Aucune table pour le moment — créez-en une !
        </p>
      </div>
    </div>

    <!-- ================= ROUND RECAP ================= -->
    <div v-else-if="flags.round_over_screen" class="recap">
      <button class="chat-toggle recap__chat-toggle" data-testid="round-recap-chat-toggle" @click="toggleChat"
              :aria-label="'Discussion' + (unread ? ', ' + unread + ' non lus' : '')">
        Chat
        <span v-if="unread && !chatOpen" class="chat-toggle__badge">{{ unread }}</span>
      </button>
      <div class="recap__card" role="dialog" aria-labelledby="rr-title">
        <h2 class="recap__title" id="rr-title">Fin de la manche</h2>
        <p v-if="roundOutcome" class="recap__outcome" :class="'recap__outcome--' + roundOutcome">
          {{ roundOutcome === 'won' ? 'Vous avez gagné la manche' : roundOutcome === 'lost' ? 'Vous avez perdu la manche' : 'Manche nulle' }}
        </p>
        <p class="recap__scores-title">Points faits</p>
        <div class="recap__scores" v-if="roundScores">
          <div>
            <div class="recap__score-team recap__team--nous">{{ nousLabel }}</div>
            <div class="recap__score-players">{{ teamPlayers.nous }}</div>
            <div class="recap__score-value">{{ roundScores.nous.cardPoints }}</div>
            <div class="recap__score-caption">points aux cartes</div>
            <div v-if="roundScores.nous.beloteBonus" class="recap__score-belote">+{{ roundScores.nous.beloteBonus }} Belote/Rebelote</div>
            <div class="recap__score-total">Score de la manche : {{ roundScores.nous.total }} pts</div>
          </div>
          <div>
            <div class="recap__score-team recap__team--eux">{{ euxLabel }}</div>
            <div class="recap__score-players">{{ teamPlayers.eux }}</div>
            <div class="recap__score-value">{{ roundScores.eux.cardPoints }}</div>
            <div class="recap__score-caption">points aux cartes</div>
            <div v-if="roundScores.eux.beloteBonus" class="recap__score-belote">+{{ roundScores.eux.beloteBonus }} Belote/Rebelote</div>
            <div class="recap__score-total">Score de la manche : {{ roundScores.eux.total }} pts</div>
          </div>
        </div>
        <p class="recap__contract" v-if="recapContract">
          Contrat {{ recapContract.label }} :
          <span :class="recapContract.honored ? 'ok' : 'ko'">{{ recapContract.honored ? '✓ réussi' : '✗ chuté' }}</span>
        </p>
        <p class="recap__scores-title">Score cumulé</p>
        <div class="recap__scores recap__scores--cumulative">
          <div>
            <div class="recap__score-team recap__team--nous">{{ nousLabel }}</div>
            <div class="recap__score-value">{{ nousScore }}</div>
          </div>
          <div>
            <div class="recap__score-team recap__team--eux">{{ euxLabel }}</div>
            <div class="recap__score-value">{{ euxScore }}</div>
          </div>
        </div>
        <button class="rematch-btn" data-testid="round-continue" @click="continueRound">{{ flags.game_over ? 'Voir le résultat de la partie' : 'Manche suivante' }}</button>
      </div>
    </div>

    <!-- ================= GAME OVER ================= -->
    <div v-else-if="flags.game_over" class="recap">
      <div class="confetti" v-if="confetti.length" aria-hidden="true">
        <span v-for="(c, i) in confetti" :key="i" class="confetti__piece"
              :style="{ left: c.left + '%', background: c.color, animationDelay: c.delay + 's', animationDuration: c.dur + 's', transform: 'rotate(' + c.rot + 'deg)' }"></span>
      </div>
                  <button class="chat-toggle recap__chat-toggle" data-testid="game-over-chat-toggle" @click="toggleChat"
                    :aria-label="'Discussion' + (unread ? ', ' + unread + ' non lus' : '')">
              Chat
              <span v-if="unread && !chatOpen" class="chat-toggle__badge">{{ unread }}</span>
                  </button>
      <div class="recap__card" role="dialog" aria-labelledby="go-title">
        <h2 class="recap__title" id="go-title">Partie terminée</h2>
        <div class="recap__winner">🏆 {{ winnerLabel }} l'emporte</div>
        <div class="recap__scores">
          <div><div class="recap__score-team recap__team--nous">{{ nousLabel }}</div><div class="recap__score-players">{{ teamPlayers.nous }}</div><div class="recap__score-value">{{ finalNous }}</div></div>
          <div><div class="recap__score-team recap__team--eux">{{ euxLabel }}</div><div class="recap__score-players">{{ teamPlayers.eux }}</div><div class="recap__score-value">{{ finalEux }}</div></div>
        </div>
        <button v-if="!isSpectator" class="rematch-btn" data-testid="rematch" @click="doRematch">Revanche</button>
        <button v-else class="rematch-btn" data-testid="spectator-leave-over" @click="leaveTable">Quitter</button>
      </div>
    </div>

    <!-- ================= TABLE VIEW ================= -->
    <template v-else>
      <header class="topbar">
        <span class="topbar__brand"><img src="favicon.ico" alt="" class="topbar__favicon" /> Coinche</span>
        <div class="scoreboard">
          <span class="scoreboard__team scoreboard__team--nous">
            <span class="scoreboard__label">{{ nousLabel }}</span>
            <span class="scoreboard__value">{{ nousScore }}</span>
          </span>
          <span class="scoreboard__team scoreboard__team--eux">
            <span class="scoreboard__label">{{ euxLabel }}</span>
            <span class="scoreboard__value">{{ euxScore }}</span>
          </span>
        </div>
        <button class="chat-toggle" data-testid="chat-toggle" @click="toggleChat"
                :aria-label="'Discussion' + (unread ? ', ' + unread + ' non lus' : '')">
          Chat
          <span v-if="unread && !chatOpen" class="chat-toggle__badge">{{ unread }}</span>
        </button>
      </header>

      <div class="stage">
        <main class="table-wrap" role="main" aria-label="Table de jeu">
          <div class="felt-scene">
            <div class="felt">
              <div class="felt__upright">
                <!-- Seats -->
                <seat-panel
                  v-for="s in seats"
                  :key="s.seatId"
                  :pos="s.slot"
                  :name="s.name"
                  :team-class="s.teamClass"
                  :played-card="s.playedCard"
                  :bid-mark="s.bidMark"
                  :is-turn="s.isTurn"
                  :is-dealer="s.isDealer"
                  :is-bot="s.isBot"
                  :bot-type="s.botType"
                  :connected="s.connected"
                  :trump="trumpSuit"
                  @change-bot-type="changeBotType(s.seatId)"
                ></seat-panel>

                <!-- Trick center / current bid -->
                <transition-group name="trick-card" tag="div" class="trick-area"
                                  :class="[sweepClass, { 'trick-area--sweeping': sweepClass }]">
                  <div v-for="(tc, i) in trickCards" :key="tc.slot" class="trick-card" :class="'trick-card--' + tc.slot">
                    <card :card="tc.card" :trump="trumpSuit"></card>
                  </div>
                </transition-group>
                <div v-if="!trickCards.length && currentBid" class="center-bid">
                  <div class="center-bid__label">Enchère</div>
                  <div class="center-bid__value">{{ currentBid }}</div>
                </div>

                <!-- Contract badge -->
                <div v-if="contract" class="contract-badge" :class="{ 'contract-badge--flash': badgeFlash }">{{ contract }}</div>
              </div>
            </div>

            <!-- Last trick corner -->
            <div v-if="lastTrickCells" class="last-trick" aria-label="Dernier pli">
              <div class="last-trick__title">Dernier pli</div>
              <div class="last-trick__grid">
                <template v-for="(c, i) in lastTrickCells" :key="i">
                  <card v-if="c" :card="c" :trump="trumpSuit"></card>
                  <span v-else></span>
                </template>
              </div>
            </div>
          </div>

          <!-- Hand fan (seated players only — a spectator holds no cards) -->
          <div v-if="!isSpectator" class="hand-fan">
            <div class="hand-fan__inner">
              <card
                v-for="(h, i) in handCards"
                :key="h.card"
                :card="h.card"
                :legal="h.legal"
                :illegal="h.illegal"
                :pending="h.pending"
                :interactive="h.legal"
                :shake="shakeCard === h.card"
                :trump="trumpSuit"
                :class="{ 'deal-enter': dealing }"
                :style="{ animationDelay: dealing ? (i * 60) + 'ms' : '0ms' }"
                @play="playCard"
              ></card>
            </div>
          </div>

          <!-- Spectator banner in place of the hand -->
          <div v-else class="spectator-bar" data-testid="spectator-bar">
            <span class="spectator-bar__eye" aria-hidden="true">👁</span>
            <span class="spectator-bar__label">Mode spectateur — vous observez cette partie</span>
            <button class="leave-btn" data-testid="spectator-leave" @click="leaveTable">
              Quitter
            </button>
          </div>

          <footer class="status-footer" aria-live="polite">
            <span v-if="statusMessage" class="status-footer__last">{{ statusMessage }}</span>
            <span v-if="turnText && !isSpectator" class="status-footer__turn">{{ turnText }}</span>
            <span v-if="turnSeconds != null && snapshot.whose_turn === snapshot.seat" class="turn-countdown"
                  :class="{ 'turn-countdown--urgent': turnSeconds < 60 }">⏱ {{ turnCountdown }}</span>
            <button v-if="canFillBots && !isSpectator" class="fill-bots-btn" data-testid="fill-bots"
                    :disabled="fillingBots" @click="fillBots">
              {{ fillingBots ? 'Ajout des bots…' : 'Remplir avec des bots' }}
            </button>
            <button v-if="!isSpectator" class="leave-btn" data-testid="leave-table"
                    :class="{ 'leave-btn--armed': leaveArmed }" :disabled="leaving"
                    @click="leaveTable">
              {{ leaving ? 'Départ en cours…' : leaveArmed ? 'Confirmer' : 'Quitter la table' }}
            </button>
          </footer>
        </main>

        <!-- Chat -->
        <div v-if="chatOpen" class="chat-scrim" aria-hidden="true" @click="toggleChat"></div>
        <chat-panel
          v-if="chatOpen"
          :messages="snapshot.chat_messages"
          :system-messages="snapshot.system_messages"
          :local-team="localTeam"
          :draft="chatDraft"
          @send="sendChat"
          @update:draft="chatDraft = $event"
          @close="toggleChat"
        ></chat-panel>
      </div>

      <!-- Bid panel overlay (only when the snapshot has a pending bid for me) -->
      <bid-panel
        v-if="bidRequest"
        :request="bidRequest"
        :sending="bidSending"
        @bid="submitBid"
      ></bid-panel>
    </template>

    <!-- Result screens replace the table subtree, but discussion remains a
         live table action: keep the same component available above them. -->
        <div v-if="chatOpen && (flags.round_over_screen || flags.game_over)" class="chat-scrim chat-scrim--overlay"
          aria-hidden="true" @click="toggleChat"></div>
    <chat-panel
      v-if="chatOpen && (flags.round_over_screen || flags.game_over)"
      class="chat-panel--overlay"
      :messages="snapshot.chat_messages"
      :system-messages="snapshot.system_messages"
      :local-team="localTeam"
      :draft="chatDraft"
      @send="sendChat"
      @update:draft="chatDraft = $event"
      @close="toggleChat"
    ></chat-panel>
  `,
};

await loadTableNames();
createApp(App).mount("#app");
