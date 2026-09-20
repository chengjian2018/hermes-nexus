"""Booking stages — the shared guard machinery of the outbound booking-call
patterns (install_booking / repair_booking), scenario-neutral.

迁移自 nexus-kit apps/install_booking_agent/stages.py 的机制层（守卫/推荐改写/
联系时间分流/关键词卡控澄清），节点码与话术全部抽成可重绑的类属性，由
dialogue/install_booking_route.py 与 dialogue/repair_booking_route.py 各自绑定；
两个 route 只声明差异，机制不在场景间复制。

``BookingGuardUnifiedNLU`` subclasses ``FSMUnifiedNLU`` (stages/unified.py) and
post-processes its single-call output with deterministic, zero-LLM slot
arithmetic (booking_slots.py):

1. the model picks <specific_date> / <nearest> with a visit time
   → extract the requested time from the time-augmented query annotation,
     match it against task_info["available_slots"];
     - bookable → annotate the slot (bookable / matched_slot) and let the
       transition proceed;
     - NOT bookable → reroute: next_node forced to <recommend> with a
       schedule-backed reply. Same spirit as the kernel's next_node hard
       guard: the model's illegal pick never reaches the node graph — the
       customer is never promised an unbookable time;
2. ANY transition into <recommend> (guarded reroute or the model's own
   "neither works" pick) gets its reply deterministically rewritten from the
   schedule (ScheduleRecommendNLG) — the recommendation the customer hears is
   always the real available_slots, never a model re-roll. This rewrite MUST
   live in the unified stage: FSM node transitions happen end-of-turn
   (chat._fsm_node_transition), so a node-level nlg on <recommend> would only
   fire on the NEXT turn (after the transition) and would clobber that turn's
   confirmation reply;
3. <ask_callback> (next contact time) answers are NOT visit times — the
   guard skips them (a "call me back Tuesday afternoon" callback time is
   none of the technician's business), and instead runs its own triage:
   an annotated future time within 2 weeks closes on the customer's time;
   anything unusable reroutes to <callback_default> (default 3-days-later
   proposal, two-beat close).

``ScheduleRecommendNLG`` is the deterministic zero-LLM NLG behind rule 2.

``KeywordClarifyStage`` subclasses the builtin ``ClarifyStage``
(stages/clarify) with the recall layer replaced by pure keyword gating: a FAQ
hit ⇒ "kb" mode with the entry as the single recall item; a miss ⇒ "fallback"
mode. The "mixed" ambiguous zone never fires (keyword matching is binary).
Contract preserved: trigger protocol (next_node == "clarify" + topic/keywords
slots), per-turn metadata["clarify"] reset/write, and the clarify-turn guard
downstream (chat._fsm_node_transition skips node jumps & slot merging on
triggered=True) all ride the parent behavior.

Wiring (the hermes-nexus way — stage instances injected via module slots, no
stage plugin registry):

    FSMModule(
        generate=ScenarioBookingUnifiedNLU(),   # BookingGuardUnifiedNLU 子类
        enable_clarify=True,                    # 澄清准入开关（模块级）
        clarify_stage=ScenarioKeywordClarifyStage(),
        base_nlu_prompt=SCENARIO_UNIFIED_PROMPT,  # 统一阶段模板（模块级覆盖）
    )
    Pattern(..., query=TimeAugQueryRewriter())  # pattern 级改写槽位
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from dialogue.base import DialogueContext, fill_prompt_template
from dialogue.booking_slots import (
    extract_requested_time,
    match_slot,
    parse_available_slots,
    suggest_slots,
)
from stages.clarify import ClarifyStage
from stages.recaller import MultiPathRecaller, WeightedScoreFusion
from stages.unified import FSMUnifiedNLU

logger = logging.getLogger(__name__)


# ============================================================================
# Schedule-backed recommendation NLG (deterministic, zero LLM)
# ============================================================================

class ScheduleRecommendNLG:
    """Schedule-backed recommendation NLG (deterministic, zero LLM).

    Standalone-executable (execute(ctx) -> ctx); invoked by
    BookingGuardUnifiedNLU on every transition into the recommend node.
    No schedule injected → no-op (keeps whatever reply the unified stage
    wrote; the model's phrasing is the only option left).

    Scenario variants subclass and rebind the wording class attributes
    (RECOMMEND_LEAD / UNBOOKABLE_LEAD / RECOMMEND_TAIL).
    """

    stage_name = "schedule_recommend_nlg"

    # Wording pieces (subclass-rebindable)
    UNBOOKABLE_LEAD = "师傅档期排不开了，"
    RECOMMEND_LEAD = "最近可以约 "
    RECOMMEND_TAIL = "，您看哪个时间段合适？"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        available = parse_available_slots(task_info)
        if not available:
            return ctx  # no schedule: keep the unified reply as-is

        nlu_slots = (ctx.nlu_result or {}).get("slots", {})
        prefix = ""
        if nlu_slots.get("bookable") is False:
            prefix = (
                f"您说的 {nlu_slots.get('requested_time', '这个时间')} "
                f"{self.UNBOOKABLE_LEAD}"
            )
        ctx.nlg_result = {
            "content": (
                f"{prefix}{self.RECOMMEND_LEAD}{suggest_slots(available)}"
                f"{self.RECOMMEND_TAIL}"
            ),
            "deterministic": True,
        }
        return ctx


# ============================================================================
# Guarded unified stage — booking-time hard guard + callback-time close
# ============================================================================

class BookingGuardUnifiedNLU(FSMUnifiedNLU):
    """FSM unified stage + booking-time hard guard + callback-time close
    (see module docstring).

    Scenario routes (install / repair) subclass this class and rebind the
    node-code class attributes plus the wording pieces — the guard machinery
    itself is node-graph agnostic.
    """

    # Nodes whose next_node means "a visit time was given / the nearest slot
    # was asked" — the booking-time guard applies exactly on these transitions
    BOOKING_TARGETS: frozenset = frozenset()

    # The recommend node: transitions into it get the deterministic reply
    RECOMMEND_NODE: str = ""

    # The callback node: its times are next-contact times, not visit times
    CALLBACK_NODE: str = ""

    # The default-callback node: unusable times reroute here (3 days later)
    CALLBACK_DEFAULT_NODE: str = ""

    # The end node: the callback close fires on callback → end transitions
    END_NODE: str = ""

    # Default callback delay when the customer's answer is unusable (too far
    # beyond 2 weeks / in the past / vague / not given)
    CALLBACK_DEFAULT_DAYS: int = 3

    # Wording pieces for the deterministic (zero-LLM) replies
    CALLBACK_CLOSE_TEXT: str = ""
    CALLBACK_DEFAULT_PROPOSAL: str = ""

    # The scenario's recommend NLG class (ScheduleRecommendNLG subclass)
    RECOMMEND_NLG_CLS = ScheduleRecommendNLG

    def __init__(self):
        super().__init__()
        self._recommend_nlg = self.RECOMMEND_NLG_CLS()

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        super().execute(ctx)  # the builtin single call (reply/next_node/slots)
        self._apply_booking_guard(ctx)
        self._apply_recommend_rewrite(ctx)
        self._apply_callback_close(ctx)
        return ctx

    # ------------------------------------------------------------------
    # Booking-time guard — deterministic post-processing, zero extra LLM
    # ------------------------------------------------------------------

    def _apply_booking_guard(self, ctx: DialogueContext) -> None:
        nlu_result = ctx.nlu_result or {}
        next_node = nlu_result.get("next_node", "")
        slots_out = dict(nlu_result.get("slots") or {})

        # 1. Not a booking transition (or a callback time) → untouched
        if next_node not in self.BOOKING_TARGETS:
            return
        if ctx.current_node_code == self.CALLBACK_NODE:
            return

        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        available = parse_available_slots(task_info)
        if not available:
            # No schedule injected: nothing to enforce, let the model's pick
            # through (declared wiring: guard is opt-in via task_info)
            slots_out["bookable"] = "no_schedule"
            self._write_back(ctx, slots_out)
            return

        requested = self._requested_slot(ctx)
        if requested is None:
            # No time entity in the utterance: not guardable, model's pick
            # stands (e.g. "就要最近的" [just give me the nearest] —
            # the nearest node picks the schedule's head by construction)
            self._write_back(ctx, slots_out)
            return

        _rs, _re, display = requested
        matched = match_slot(requested, available)
        if matched is not None:
            slots_out["bookable"] = True
            slots_out["matched_slot"] = matched
            slots_out["requested_time"] = display
            self._write_back(ctx, slots_out)
            return

        # 2. NOT bookable → deterministic reroute to the recommend node
        # (reply rewritten by _apply_recommend_rewrite below)
        slots_out["bookable"] = False
        slots_out["requested_time"] = display
        ctx.nlu_result = {
            "next_node": self.RECOMMEND_NODE,
            "slots": slots_out,
        }
        meta = dict(ctx.metadata.get("unified") or {})
        meta["booking_guard"] = {
            "requested": display,
            "bookable": False,
            "rerouted_to": self.RECOMMEND_NODE,
        }
        ctx.metadata["unified"] = meta
        logger.info(
            "[booking] 可约时间守卫改道: 请求 %s 不可约 → 推荐 %s",
            display, suggest_slots(available),
        )

    def _apply_recommend_rewrite(self, ctx: DialogueContext) -> None:
        """Deterministic recommend reply on EVERY transition into the
        recommend node (guarded reroute or the model's own pick)."""
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node == self.RECOMMEND_NODE:
            self._recommend_nlg.execute(ctx)

    def _requested_slot(self, ctx: DialogueContext):
        """The customer's requested time from the time-augmented query.

        Preference: the rewritten query's annotations (absolute times); a
        time phrase the model echoed into slots (visit_date/visit_hour)
        without any annotation falls back to raw matching.
        """
        rewritten = (ctx.rewritten_queries or [""])[0] or ""
        today = self._now_datetime(ctx).strftime("%Y-%m-%d")
        slot = extract_requested_time(rewritten, today)
        if slot is not None:
            return slot
        echoed = " ".join(
            str((ctx.nlu_result or {}).get("slots", {}).get(k) or "")
            for k in ("visit_date", "visit_hour", "visit_time")
        )
        if echoed.strip():
            return extract_requested_time(echoed, today)
        return None

    def _write_back(self, ctx: DialogueContext, slots_out: Dict[str, Any]) -> None:
        """Rewrite nlu_result.slots with the guard's annotations."""
        nlu_result = dict(ctx.nlu_result or {})
        nlu_result["slots"] = slots_out
        ctx.nlu_result = nlu_result

    # ------------------------------------------------------------------
    # Callback-time close — next-contact time triage, deterministic (zero LLM)
    # ------------------------------------------------------------------
    # The customer answered <ask_callback> with a next-CONTACT time.
    # Branches (aligned with the time_augment 2-week annotation window, and
    # carried by the node graph — ask_callback's sub_nodes):
    #   annotated time in the rewritten query  → a valid future time within
    #     2 weeks: keep the transition to <end> and restate the
    #     customer's time in the goodbye (branch ②, one beat);
    #   no annotation                          → too far (beyond 2 weeks) /
    #     in the past / vague ("都行" [whatever works]) / not given: REROUTE to
    #     <callback_default> (branch ①③) — the reply proposes the
    #     default 3-days-later callback and asks; the customer's answer on
    #     that node closes the call (two beats, same shape as the decline
    #     channel). The time_aug query slot only annotates future times
    #     ending within the 2-week window, so "annotated vs not" IS the
    #     branch decision — no second parsing layer needed.

    def _apply_callback_close(self, ctx: DialogueContext) -> None:
        if ctx.current_node_code != self.CALLBACK_NODE:
            # On the default-callback node: the customer answered the
            # proposal — close with the default time restated (the slots
            # were recorded on the rerouting turn)
            self._close_default_callback(ctx)
            return
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node != self.END_NODE:
            return  # not closing yet (e.g. clarify signal): untouched

        now = self._now_datetime(ctx)
        annotated = extract_requested_time(
            (ctx.rewritten_queries or [""])[0] or "",
            now.strftime("%Y-%m-%d"))
        slots_out = dict((ctx.nlu_result or {}).get("slots") or {})

        if annotated is not None:
            # Branch ②: valid customer time — close on it directly
            _s, _e, display = annotated
            slots_out["callback_time"] = display
            slots_out["callback_source"] = "customer"
            ctx.nlg_result = {"content": (
                f"好的，那我们就 {display} 再联系您～"
                f"{self.CALLBACK_CLOSE_TEXT}")}
            self._write_back(ctx, slots_out)
            return

        # Branch ①③: unusable time — reroute to the default-callback node
        # with the deterministic proposal (zero extra LLM)
        default_day = (now + timedelta(
            days=self.CALLBACK_DEFAULT_DAYS)).strftime("%Y-%m-%d")
        slots_out["callback_time"] = default_day
        slots_out["callback_source"] = "default"
        ctx.nlu_result = {
            "next_node": self.CALLBACK_DEFAULT_NODE,
            "slots": slots_out,
        }
        ctx.nlg_result = {"content": self.CALLBACK_DEFAULT_PROPOSAL.format(
            day=default_day)}
        meta = dict(ctx.metadata.get("unified") or {})
        meta["callback_guard"] = {
            "requested": (ctx.rewritten_queries or [ctx.user_query])[0],
            "usable": False,
            "default_day": default_day,
            "rerouted_to": self.CALLBACK_DEFAULT_NODE,
        }
        ctx.metadata["unified"] = meta
        logger.info(
            "[booking] 联系时间不可用，改道默认改约三天: %s",
            default_day,
        )

    def _close_default_callback(self, ctx: DialogueContext) -> None:
        """On <callback_default> heading for end: restate the default
        callback time recorded on the rerouting turn (deterministic)."""
        if ctx.current_node_code != self.CALLBACK_DEFAULT_NODE:
            return
        next_node = (ctx.nlu_result or {}).get("next_node", "")
        if next_node != self.END_NODE:
            return
        default_day = ctx.filled_slots.get("callback_time")
        if default_day:
            ctx.nlg_result = {"content": (
                f"好的，那我们就 {default_day} 再联系您～"
                f"{self.CALLBACK_CLOSE_TEXT}")}

    @staticmethod
    def _now_datetime(ctx) -> datetime:
        """The turn's time base (tests inject metadata.time_base)."""
        tb = ctx.metadata.get("time_base")
        if tb:
            return datetime.fromtimestamp(tb)
        return datetime.now()


# ============================================================================
# Keyword-gated clarify stage — recall replaced by pure keyword containment
# ============================================================================

class KeywordClarifyStage(ClarifyStage):
    """Dual-track clarify with the recall layer replaced by pure keyword
    gating (business detection is gated by keywords only).

    What changes vs the builtin ClarifyStage:

    - Detection: no MultiPathRecaller recall / ClarifyRouteRule gating — the
      assembled search text (user query + topic + keywords) goes through the
      scenario's FAQ matcher (specific-first keyword containment). A hit ⇒
      "kb" mode with the FAQ entry as the single recall item; a miss ⇒
      "fallback" mode. The "mixed" ambiguous zone never fires.
    - Generation: keeps the parent's LLM call but with phone-call-shaped
      templates — the kb template gets the FAQ answer pre-filled
      ({__faq_answer__}), so the model only phrases the acknowledge +
      pull-back around a fixed fact, never re-answers.
    - Contract preserved: trigger protocol (next_node == "clarify" +
      topic/keywords slots), per-turn metadata["clarify"] reset/write, and
      the clarify-turn guard downstream (chat._fsm_node_transition skips
      node jumps & slot merging on triggered=True) all ride the parent
      behavior.

    Scenario routes subclass and rebind the FAQ keyword table
    (FAQ_MATCHER), the kb/fallback prompt templates (CLARIFY_PROMPTS) and
    the generate-failure fallback line (phone-call shaped per scenario).
    """

    stage_name = "keyword_clarify"

    # Scenario-rebindable pieces (a route forgetting to rebind FAQ_MATCHER /
    # CLARIFY_PROMPTS fails loudly on first clarify turn — no silent defaults)
    FAQ_MATCHER = None          # (text) -> Optional[entry dict]
    CLARIFY_PROMPTS: Dict[str, str] = {}
    CLARIFY_FALLBACK_REPLY = ""

    def __init__(self):
        # The parent constructor demands a recaller (the builtin recall
        # layer); keyword gating never invokes it, so a bare MultiPathRecaller
        # with no paths is passed as a placeholder — a no-op even if
        # accidentally run.
        super().__init__(recaller=MultiPathRecaller(
            recall_paths=[], filters=[], fusion=WeightedScoreFusion()))

    def _keyword_route(self, ctx: DialogueContext, open_slots: Dict[str, Any]):
        """Keyword-only gating: returns (mode, recall_items, search_query).

        A FAQ hit becomes the single recall item carrying the entry's
        answer; a miss returns fallback with an empty list — the shapes the
        parent's prompt assembly already understands.
        """
        search_query = self._build_search_query(ctx, open_slots)
        entry = self.FAQ_MATCHER(search_query)
        if entry is None:
            return "fallback", [], search_query
        answer = str(entry["answer"])
        item = {
            "id": f"faq:{entry['topic']}",
            "content": answer,
            "score": 1.0,  # keyword hit is binary; score only for observability
            "metadata": {"keywords": list(entry["keywords"])},  # type: ignore[arg-type]
        }
        return "kb", [item], search_query

    def _fill_faq_answer(self, prompt: str, ctx: DialogueContext) -> str:
        """Substitute task_info fields into the FAQ answer's {field}
        placeholders ({product_name} etc.), then inject it into the prompt's
        {__faq_answer__} slot (the kb template carries {__faq_answer__}
        instead — this fills it; plain replace, same as fill_prompt_template).
        """
        task_info = ctx.task_basic_info or ctx.metadata.get("task_info") or {}
        recall = (ctx.metadata.get("clarify") or {}).get("recall_results") or []
        answer = str(recall[0].get("content", "")) if recall else ""
        for key, value in task_info.items():
            answer = answer.replace("{" + str(key) + "}", str(value))
        return prompt.replace("{__faq_answer__}", answer)

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        # 1. Per-turn reset (same contract as the parent)
        ctx.metadata["clarify"] = {"triggered": False}

        if not self._is_triggered(ctx):
            return ctx

        open_slots = self._extract_open_slots(ctx)

        # 2-3. Keyword-only detection (replaces recall + score gating)
        mode, recall_items, search_query = self._keyword_route(ctx, open_slots)
        logger.info(
            "澄清关键词卡控: session=%s, mode=%s, query=%r",
            ctx.session_id, mode, search_query,
        )

        # 4. Generate by mode (the only NLG call this turn) with the FAQ
        # answer pre-filled — write metadata first so _fill_faq_answer reads it
        ctx.metadata["clarify"] = {
            "triggered": True,
            "mode": mode,
            "recall_results": recall_items,
            "open_slots": open_slots,
            "query": search_query,
        }
        template = self.CLARIFY_PROMPTS.get(
            mode, self.CLARIFY_PROMPTS.get("fallback", ""))
        prompt = self._build_custom_prompt(ctx, open_slots,
                                           recall_items, template)
        prompt = self._fill_faq_answer(prompt, ctx)
        try:
            content = self._generate(prompt, ctx.llm_config).strip()
        except Exception as e:
            logger.warning("澄清生成异常，使用兜底话术: %s", e, exc_info=True)
            content = self.CLARIFY_FALLBACK_REPLY
        ctx.nlg_result = {"content": content}
        return ctx

    def _build_custom_prompt(self, ctx: DialogueContext,
                             open_slots: Dict[str, Any],
                             recall_items: list, template: str) -> str:
        """Assemble the custom template with the parent's slot vocabulary."""
        slots = {
            "query": ctx.user_query,
            "topic": open_slots["topic"] or "（无）",
            "keywords": "、".join(open_slots["keywords"]) or "（无）",
            "recall_info": self._format_recall_for_prompt(recall_items),
            "cur_node": ctx.format_cur_node(stage="nlg"),
            "history": ctx.format_history(),
            "task_info": ctx.format_task_info(),
        }
        return fill_prompt_template(template, slots)
