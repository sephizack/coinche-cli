// =========================================================================
// Coinche — Casino web overlay (U3)
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
//   play | bid | chat | join | rematch | lobby   (card play is "play", NOT
//   "play_card" — play_card is the game-wire type, not the browser action).
// =========================================================================

const { createApp, ref, reactive, computed, watch, nextTick, onMounted } = Vue;

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
    interactive: { type: Boolean, default: false },
  },
  emits: ["play"],
  computed: {
    parts() {
      return splitCard(this.card);
    },
    isRed() {
      return RED_SUITS.has(this.parts.suit);
    },
    label() {
      return this.faceUp ? cardLabel(this.card) : "Carte face cachée";
    },
    classes() {
      return {
        "card--legal": this.legal,
        "card--illegal": this.illegal,
        "card--shake": this.shake,
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
    connected: { type: Boolean, default: true },
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
      <div class="seat__nameplate">
        <span class="seat__name">{{ name }}</span>
        <span v-if="isDealer" class="seat__badge">(D)</span>
      </div>
      <span v-if="!connected" class="seat__offline-note">déconnecté</span>
      <div class="seat__slot">
        <card v-if="playedCard" :card="playedCard"></card>
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
    return { selectedTrump: null, pointsIndex: 0 };
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
      if (!this.selectedTrump) return [];
      const pts = this.legalActions
        .filter((a) => a.trump === this.selectedTrump)
        .map((a) => a.points);
      const numeric = pts.filter((p) => p !== "capot").sort((a, b) => a - b);
      const list = numeric.slice();
      if (pts.includes("capot")) list.push("capot");
      return list;
    },
    currentPoints() {
      return this.pointsForTrump[this.pointsIndex];
    },
    currentPointsLabel() {
      return this.currentPoints === "capot" ? "Capot" : this.currentPoints;
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
    pass() {
      this.$emit("bid", { bid_action: "pass" });
    },
    coinche() {
      this.$emit("bid", { bid_action: "coinche" });
    },
    surcoinche() {
      this.$emit("bid", { bid_action: "surcoinche" });
    },
  },
  mounted() {
    // Focus into the dialog (a11y: BidPanel role=dialog with focus handling).
    nextTick(() => {
      const first = this.$el.querySelector("button:not(:disabled)");
      if (first) first.focus();
    });
  },
  template: `
    <div class="scrim">
      <div class="bid-panel" :class="{ 'bid-panel--sending': sending }"
           role="dialog" aria-modal="true" aria-labelledby="bid-title">
        <h2 class="bid-panel__title" id="bid-title">À vous d'annoncer</h2>
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
            <span class="points-value" :class="{ 'points-value--capot': currentPoints === 'capot' }"
                  data-testid="bid-points">{{ currentPointsLabel }}</span>
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
    localTeam: String, // NS | EW
  },
  emits: ["send", "close"],
  data() {
    return { draft: "" };
  },
  computed: {
    view() {
      return (this.messages || []).map((m) => ({
        name: m.name,
        text: m.text,
        cls: m.team === this.localTeam ? "nous" : "eux",
      }));
    },
  },
  methods: {
    submit() {
      const text = this.draft.trim().slice(0, 256); // UX cap only; server is authoritative
      if (!text) return;
      this.$emit("send", text);
      this.draft = "";
    },
  },
  mounted() {
    // Opening the chat moves focus into it (a11y).
    nextTick(() => {
      const input = this.$el.querySelector(".chat-input");
      if (input) input.focus();
    });
  },
  updated() {
    const log = this.$el.querySelector(".chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  },
  template: `
    <aside class="chat-panel" role="region" aria-label="Discussion">
      <div class="chat-panel__header">
        <span>Discussion</span>
        <button class="chat-panel__close" aria-label="Fermer la discussion" @click="$emit('close')">×</button>
      </div>
      <div class="chat-log" aria-live="polite">
        <p v-if="!view.length" class="chat-empty">Aucun message.</p>
        <div v-for="(m, i) in view" :key="i" class="chat-msg">
          <span class="chat-msg__name" :class="'chat-msg__name--' + m.cls">{{ m.name }}</span>
          <span class="chat-msg__text">{{ m.text }}</span>
        </div>
      </div>
      <form class="chat-compose" @submit.prevent="submit">
        <input class="chat-input" type="text" maxlength="256" v-model="draft"
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
    const unread = ref(0);
    const bidSending = ref(false);
    const shakeCard = ref(null);
    const dealing = ref(false);
    const badgeFlash = ref(false);
    const sweepClass = ref(null); // e.g. "sweep-north" while a trick sweeps out
    const confetti = ref([]);
    // Lobby form (there is no table-list in the snapshot contract; U2 pushes
    // players/status only — so the lobby is a join form driven by that state).
    const lobby = reactive({ name: "", table: "table1", team: "" });

    let ws = null;
    let backoff = 500;
    let toastId = 0;

    // -------- WebSocket (ConnectionLayer) --------
    function wsUrl() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      return `${proto}://${location.host}/ws`;
    }

    function connect() {
      ws = new WebSocket(wsUrl());
      ws.addEventListener("open", () => {
        backoff = 500;
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
        }
      });
      ws.addEventListener("close", () => {
        showToast("reconnexion…", "info", 4000);
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 4000);
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

      // Trick sweep (two-stage flip): when the just-completed 4-card trick
      // clears, sweep the cards toward the winner. The winner is whose_turn at
      // the moment the trick was full (TRICK_RESULT), captured from prev.
      if (prev && !REDUCED_MOTION) {
        const prevTrick = Object.keys(prev.current_trick || {}).length;
        const nowTrick = Object.keys(snap.current_trick || {}).length;
        if (prevTrick === 4 && nowTrick === 0 && prev.whose_turn) {
          const dir = visualPosition(prev.whose_turn, snap.seat || prev.seat);
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

      snapshot.value = snap; // full replace
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
      toasts.value.push({ id, message, type });
      setTimeout(() => {
        toasts.value = toasts.value.filter((t) => t.id !== id);
      }, ttl);
    }

    // -------- derived view state --------
    const joined = computed(() => snapshot.value && snapshot.value.seat);
    const flags = computed(
      () => (snapshot.value && snapshot.value.flags) || {},
    );
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
      if (!s || !s.seat) return [];
      const players = s.players || {};
      const teamOf = s.team_of || {};
      const trick = s.current_trick || {};
      const marks = s.bid_marks || {};
      const conn = s.connection_status || {};
      return Object.keys(players).map((seatId) => {
        return {
          seatId,
          slot: visualPosition(seatId, s.seat),
          name: players[seatId],
          teamClass: teamOf[seatId] === localTeam.value ? "nous" : "eux",
          playedCard: trick[seatId] || null,
          bidMark: marks[seatId] || null,
          isTurn: s.whose_turn === seatId,
          isDealer: s.dealer_seat === seatId,
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
        slot: visualPosition(seatId, s.seat),
      }));
    });

    // Last-trick corner (compact 3x3, mirrors ui.last_trick_grid).
    const lastTrickCells = computed(() => {
      const s = snapshot.value;
      if (!s || !s.last_trick || !Object.keys(s.last_trick).length) return null;
      const grid = Array(9).fill(null); // slots: 1=N,3=W,5=E,7=S in a 3x3
      const slotIndex = { north: 1, west: 3, east: 5, south: 7 };
      for (const seatId of Object.keys(s.last_trick)) {
        const slot = visualPosition(seatId, s.seat);
        grid[slotIndex[slot]] = s.last_trick[seatId];
      }
      return grid;
    });

    const handCards = computed(() => {
      const s = snapshot.value;
      if (!s) return [];
      const canPlay = !!s.pending_play_request;
      // Like the CLI: every card is presented as choosable during our turn
      // (no greying of "illegal" cards). Legality is checked on click and a
      // rejection message is shown instead — the server stays authoritative.
      return (s.hand || []).map((card) => ({
        card,
        legal: canPlay, // clickable/interactive on our turn
        illegal: false, // never dim cards — all look playable
      }));
    });

    const contract = computed(() => {
      const s = snapshot.value;
      if (!s || !s.trump || s.contract_points == null) return null;
      const pts = s.contract_points === "capot" ? "Capot" : s.contract_points;
      let label = `Annonce : ${pts} ${s.trump}`;
      if (s.coinche_level > 1) label += ` x${s.coinche_level}`;
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

    const turnText = computed(() => {
      const s = snapshot.value;
      if (!s || !s.whose_turn) return "";
      if (s.whose_turn === s.seat) return "À vous de jouer";
      const who = (s.players || {})[s.whose_turn] || s.whose_turn;
      return "Au tour de " + who;
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
      // Per-team dict from rules.score_round carries the manche score under `total`.
      const val = (team) => {
        const t = rs[team];
        if (t == null) return 0;
        return typeof t === "object" ? (t.total ?? 0) : t;
      };
      return { nous: val(localTeam.value), eux: val(otherTeam.value) };
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
      if (!s || !s.pending_play_request) return; // not our turn: ignore
      const legal = new Set(s.legal_cards || []);
      if (!legal.has(card)) {
        // Illegal card: like the CLI, let the player try, then tell them it's
        // not allowed (server stays authoritative — nothing is sent).
        shakeCard.value = card;
        setTimeout(() => (shakeCard.value = null), 400);
        showToast(
          `Impossible de jouer ${card} maintenant (carte non autorisée).`,
          "error",
          3500,
        );
        return;
      }
      sendAction("play", { card });
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
    function joinTable() {
      if (!lobby.name.trim() || !lobby.table.trim()) return;
      const payload = {
        table_key: lobby.table.trim(),
        player_name: lobby.name.trim(),
      };
      if (lobby.team.trim()) payload.team_name = lobby.team.trim();
      sendAction("join", payload);
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

    watch(chatOpen, (open) => {
      if (open) unread.value = 0;
    });

    onMounted(connect);

    return {
      snapshot,
      toasts,
      chatOpen,
      unread,
      bidSending,
      shakeCard,
      dealing,
      badgeFlash,
      sweepClass,
      confetti,
      lobby,
      REDUCED_MOTION,
      joined,
      flags,
      localTeam,
      nousLabel,
      euxLabel,
      nousScore,
      euxScore,
      seats,
      trickCards,
      lastTrickCells,
      handCards,
      contract,
      currentBid,
      bidRequest,
      turnText,
      recapContract,
      roundScores,
      winnerLabel,
      finalNous,
      finalEux,
      statusMessage,
      lobbyTeams,
      playCard,
      submitBid,
      sendChat,
      doRematch,
      joinTable,
      toggleChat,
    };
  },
  template: `
    <!-- Toasts (transient errors / reconnection notice) -->
    <div class="toast-stack" aria-live="assertive">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="{ 'toast--info': t.type === 'info' }">{{ t.message }}</div>
    </div>

    <!-- ================= LOBBY (not joined) ================= -->
    <div v-if="!joined" class="lobby">
      <div class="lobby__card">
        <h1 class="lobby__title">Coinche — Casino</h1>
        <div class="lobby__field">
          <label for="lobby-name">Votre nom</label>
          <input id="lobby-name" type="text" v-model="lobby.name" maxlength="24"
                 data-testid="lobby-name" placeholder="Alice" />
        </div>
        <div class="lobby__field">
          <label for="lobby-table">Table</label>
          <input id="lobby-table" type="text" v-model="lobby.table" maxlength="24"
                 data-testid="lobby-table" placeholder="table1" />
        </div>

        <div class="team-picker">
          <div class="team-card team-card--nous">
            <div class="team-card__title">{{ lobbyTeams.nsLabel }}</div>
            <ul class="team-card__members">
              <li v-for="(n, i) in lobbyTeams.ns" :key="'ns'+i">{{ n }}</li>
              <li v-if="!lobbyTeams.ns.length" class="free">(libre)</li>
            </ul>
            <button class="team-join" data-testid="join-ns"
                    @click="lobby.team = lobbyTeams.nsLabel; joinTable()">Rejoindre</button>
          </div>
          <div class="team-card team-card--eux">
            <div class="team-card__title">{{ lobbyTeams.ewLabel }}</div>
            <ul class="team-card__members">
              <li v-for="(n, i) in lobbyTeams.ew" :key="'ew'+i">{{ n }}</li>
              <li v-if="!lobbyTeams.ew.length" class="free">(libre)</li>
            </ul>
            <button class="team-join" data-testid="join-ew"
                    @click="lobby.team = lobbyTeams.ewLabel; joinTable()">Rejoindre</button>
          </div>
        </div>

        <button class="rematch-btn" style="width:100%;margin-top:0" data-testid="lobby-join"
                @click="joinTable()">Rejoindre la table</button>
      </div>
    </div>

    <!-- ================= GAME OVER ================= -->
    <div v-else-if="flags.game_over" class="recap">
      <div class="confetti" v-if="confetti.length" aria-hidden="true">
        <span v-for="(c, i) in confetti" :key="i" class="confetti__piece"
              :style="{ left: c.left + '%', background: c.color, animationDelay: c.delay + 's', animationDuration: c.dur + 's', transform: 'rotate(' + c.rot + 'deg)' }"></span>
      </div>
      <div class="recap__card" role="dialog" aria-labelledby="go-title">
        <h2 class="recap__title" id="go-title">Partie terminée</h2>
        <div class="recap__winner">🏆 {{ winnerLabel }} l'emporte</div>
        <div class="recap__scores">
          <div><div class="recap__score-team recap__team--nous">{{ nousLabel }}</div><div class="recap__score-value">{{ finalNous }}</div></div>
          <div><div class="recap__score-team recap__team--eux">{{ euxLabel }}</div><div class="recap__score-value">{{ finalEux }}</div></div>
        </div>
        <button class="rematch-btn" data-testid="rematch" @click="doRematch">Revanche</button>
      </div>
    </div>

    <!-- ================= ROUND RECAP ================= -->
    <div v-else-if="flags.round_over_screen" class="recap">
      <div class="recap__card" role="dialog" aria-labelledby="rr-title">
        <h2 class="recap__title" id="rr-title">Fin de la manche</h2>
        <div class="recap__scores" v-if="roundScores">
          <div><div class="recap__score-team recap__team--nous">{{ nousLabel }}</div><div class="recap__score-value">{{ roundScores.nous }}</div></div>
          <div><div class="recap__score-team recap__team--eux">{{ euxLabel }}</div><div class="recap__score-value">{{ roundScores.eux }}</div></div>
        </div>
        <p class="recap__contract" v-if="recapContract">
          Contrat {{ recapContract.label }} :
          <span :class="recapContract.honored ? 'ok' : 'ko'">{{ recapContract.honored ? '✓ réussi' : '✗ chuté' }}</span>
        </p>
        <p class="recap__contract">Score cumulé — {{ nousLabel }} {{ nousScore }} / {{ euxLabel }} {{ euxScore }}</p>
      </div>
    </div>

    <!-- ================= TABLE VIEW ================= -->
    <template v-else>
      <header class="topbar">
        <span class="topbar__brand">Coinche</span>
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
                  :connected="s.connected"
                ></seat-panel>

                <!-- Trick center / current bid -->
                <div class="trick-area" :class="[sweepClass, { 'trick-area--sweeping': sweepClass }]">
                  <div v-for="(tc, i) in trickCards" :key="tc.slot" class="trick-card" :class="'trick-card--' + tc.slot">
                    <card :card="tc.card"></card>
                  </div>
                </div>
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
                  <card v-if="c" :card="c"></card>
                  <span v-else></span>
                </template>
              </div>
            </div>
          </div>

          <!-- Hand fan -->
          <div class="hand-fan">
            <div class="hand-fan__inner">
              <card
                v-for="(h, i) in handCards"
                :key="h.card"
                :card="h.card"
                :legal="h.legal"
                :illegal="h.illegal"
                :interactive="h.legal"
                :shake="shakeCard === h.card"
                :class="{ 'deal-enter': dealing }"
                :style="{ animationDelay: dealing ? (i * 60) + 'ms' : '0ms' }"
                @play="playCard"
              ></card>
            </div>
          </div>

          <footer class="status-footer" aria-live="polite">
            <span v-if="statusMessage" class="status-footer__last">{{ statusMessage }}</span>
            <span v-if="turnText" class="status-footer__turn">{{ turnText }}</span>
          </footer>
        </main>

        <!-- Chat -->
        <chat-panel
          v-if="chatOpen"
          :messages="snapshot.chat_messages"
          :local-team="localTeam"
          @send="sendChat"
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
  `,
};

createApp(App).mount("#app");
