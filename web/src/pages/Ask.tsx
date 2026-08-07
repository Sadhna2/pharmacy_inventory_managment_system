/**
 * Ask.
 *
 * A question typed in English, answered with rows out of this database.
 *
 * WHAT IS ON THE SCREEN AND WHERE IT CAME FROM
 * --------------------------------------------
 * The model never answers. It proposes one SELECT, `ai/ask/safety.py` decides
 * whether that statement may run, and the database plans it before it runs —
 * so everything below a question is either rows Postgres returned or a
 * sentence the model wrote *about a query anyone here can read*. That is the
 * whole reason the SQL is always one click away and never further than one:
 * an answer nobody can check is an answer nobody should act on, and the person
 * best placed to spot the wrong branch in a WHERE clause is the pharmacist who
 * knows which branch they meant.
 *
 * VOICE FILLS THE BOX. IT NEVER SENDS.
 * ------------------------------------
 * Dictation hears "Amoxicillin" as "a mock sicilian" often enough that
 * submitting what it heard would ask a different question and then put a
 * confident, correct-looking answer underneath it. Every one of those reads as
 * the tool being wrong. So speech lands in the text box, the person reads it,
 * and the person presses Ask.
 *
 * THE THREAD REMEMBERS EXACTLY ONE STEP
 * -------------------------------------
 * A follow-up is sent with the turn immediately above it and nothing older.
 * Not a transcript: with depth the model starts dropping a filter set two
 * questions ago and returns a smaller, entirely believable number that nobody
 * questions. "Start over" exists so a genuinely new topic cannot inherit an
 * old filter, and a refine turn shows both statements side by side, because
 * memory you can see is the only kind worth trusting.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  Copy,
  Database,
  MessagesSquare,
  Mic,
  RotateCcw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkle,
  Split,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn, qty } from "@/lib/format";
import { useVoice, type VoiceProblem } from "@/lib/useVoice";
import { PageHeader } from "@/components/Shell";
import AnswerView, { type ChartHint } from "@/components/AnswerView";
import {
  AiBadge,
  Badge,
  Button,
  Card,
  CardHeader,
  ErrorState,
  Spinner,
  Textarea,
} from "@/components/ui";

/**
 * The endpoint's shapes, written out rather than aliased from `lib/types.ts`.
 *
 * Every other type in this app comes from `schema.d.ts`, which is generated
 * from the live OpenAPI document, and these belong there too — they will move
 * the first time `npm run gen:api` runs against a server carrying this route.
 * Until then they mirror `api/app/ai/ask/schemas.py` field for field, which is
 * the one file to change them against.
 */
type Outcome = "answer" | "refuse" | "clarify";

/** The whole of the memory: the previous question and the SQL it produced. */
interface PreviousTurn {
  question: string;
  sql: string;
}

interface AskIn {
  question: string;
  previous?: PreviousTurn;
}

interface AskAnswer {
  outcome: Outcome;
  question: string;
  mode: "new" | "refine";
  /** Present even on a refusal — a query written and declined is a fact. */
  sql: string | null;
  previous_sql: string | null;
  explanation: string;
  assumptions: string[];
  confidence: number | null;
  clarifying_question: string | null;
  refusal: string | null;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  elapsed_ms: number;
  chart_hint: ChartHint;
  summary: string;
}

interface Turn {
  id: number;
  answer: AskAnswer;
}

/** Mirrors `service.MAX_QUESTION_CHARS`; the server rejects anything longer. */
const MAX_QUESTION_CHARS = 500;

/**
 * How tall the box may grow before it scrolls inside itself: about eight
 * lines. Past that the composer starts eating the answer above it, and a
 * question that long is being pasted rather than written.
 */
const MAX_COMPOSER_PX = 176;

/**
 * The one turn a follow-up is allowed to know about.
 *
 * The turn immediately above, and only when it produced a statement that ran.
 * Deliberately not "the most recent turn that worked": stepping back over a
 * refusal to reach an older query is how one step quietly becomes two, and a
 * filter from three questions ago comes back to narrow an answer nobody thinks
 * to check.
 */
function memoryOf(thread: Turn[]): PreviousTurn | undefined {
  const last = thread.at(-1)?.answer;
  if (!last || last.outcome !== "answer" || !last.sql) return undefined;
  return { question: last.question, sql: last.sql };
}

/* ------------------------------------------------------------------- pieces */

/**
 * Copy, with the button reporting that it worked.
 *
 * The confirmation is the whole point. A copy button that looks identical
 * before and after leaves the person pressing it twice and then pasting
 * somewhere else to check, and the statement they are copying is the evidence
 * for a number they are about to act on.
 */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1400);
        });
      }}
      className={cn(
        "flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] transition-colors",
        copied ? "text-ok-strong" : "text-ink-faint hover:bg-muted hover:text-ink",
      )}
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function SqlBlock({ label, sql }: { label?: string; sql: string }) {
  return (
    <div className="group/sql min-w-0">
      <div className="mb-1 flex items-center gap-2">
        {label && (
          <p className="text-[11px] tracking-wide text-ink-faint uppercase">
            {label}
          </p>
        )}
        <span className="ml-auto opacity-0 transition-opacity group-hover/sql:opacity-100 focus-within:opacity-100">
          <CopyButton text={sql} label="Copy this SQL" />
        </span>
      </div>
      {/* `.scroll-x` so a wide statement scrolls inside its own box and the
          page never does. */}
      <pre
        className={cn(
          "scroll-x rounded-md border border-line bg-surface px-3 py-2",
          "font-mono text-[12px] leading-relaxed text-ink",
        )}
      >
        <code>{sql}</code>
      </pre>
    </div>
  );
}

/**
 * The statement, the row count and the time it took — collapsed.
 *
 * Collapsed rather than absent, and never behind a setting: the pharmacist who
 * wants the number gets the number, and the one who wants to know which
 * branches were counted opens one disclosure. What stays visible while it is
 * shut is the part that changes how the answer reads — how many rows came
 * back, and whether that was all of them.
 */
function QueryDetails({ answer }: { answer: AskAnswer }) {
  if (!answer.sql) return null;

  const meta =
    answer.outcome === "refuse"
      ? "written, then declined"
      : `${answer.truncated ? "first " : ""}${qty(answer.row_count)} ${
          answer.row_count === 1 ? "row" : "rows"
        } · ${qty(answer.elapsed_ms)} ms in the database`;

  return (
    <details className="group rounded-lg border border-line bg-muted/40">
      <summary
        className={cn(
          "flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2",
          "text-[13px] text-ink-soft transition-colors select-none hover:text-ink",
          // The default disclosure triangle sits outside the padding and lands
          // in a different place in every browser; the chevron below replaces it.
          "list-none [&::-webkit-details-marker]:hidden",
        )}
      >
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" />
        Show the SQL
        <span className="tnum ml-auto text-right text-[12px] text-ink-faint">
          {meta}
        </span>
      </summary>

      <div className="space-y-3 border-t border-line px-3 py-3">
        {answer.previous_sql ? (
          // Both statements, so "this refines that" is something the reader
          // can check rather than something the screen asserts.
          <div className="grid gap-3 lg:grid-cols-2">
            <SqlBlock label="The question before this one" sql={answer.previous_sql} />
            <SqlBlock label="This question" sql={answer.sql} />
          </div>
        ) : (
          <SqlBlock sql={answer.sql} />
        )}

        {answer.truncated && (
          <p className="text-[12px] text-warn">
            The row cap was reached, so there may be more than is shown here.
            Narrowing the question, or asking for a total instead of a list,
            gets the whole answer.
          </p>
        )}

        {answer.confidence !== null && (
          <p className="text-[12px] text-ink-faint">
            The model rated its own answer {answer.confidence.toFixed(2)} out of 1.
            That is a self-report and nothing here acts on it — the query either
            passed the safety check and ran, or it did not.
          </p>
        )}
      </div>
    </details>
  );
}

/**
 * The choices the question left open and the query settled.
 *
 * Shown next to the answer rather than inside the disclosure, because this is
 * where somebody catches a confident answer to a question they did not ask —
 * "assumed the Andheri branch" is the whole difference between right and
 * plausible.
 */
/**
 * The choices the question left open, behind a disclosure.
 *
 * Collapsed, not removed. Four assumptions printed under every answer is four
 * lines of the same shape on every turn, and a block that always looks the
 * same stops being read by the third one — so the turn it actually mattered on
 * goes past unnoticed too. The count stays on the summary line, because "3
 * assumptions" is the part that decides whether to open it.
 */
function Assumptions({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <details className="group rounded-lg border border-line bg-muted/40">
      <summary
        className={cn(
          "flex cursor-pointer items-center gap-2 rounded-lg px-3 py-1.5",
          "text-[12px] text-ink-soft transition-colors select-none hover:text-ink",
          "list-none [&::-webkit-details-marker]:hidden",
        )}
      >
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" />
        What it decided for you
        <span className="tnum ml-auto text-[12px] text-ink-faint">
          {items.length}
        </span>
      </summary>
      <ul className="space-y-1 border-t border-line px-3 py-2">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-[13px] text-ink-soft">
            <span className="mt-[7px] size-1 shrink-0 rounded-full bg-ink-faint" />
            {item}
          </li>
        ))}
      </ul>
    </details>
  );
}

/**
 * One question and what came back of it.
 *
 * A refusal and a question back are laid out like the answer they are — plain
 * type on the same surface, an outline icon, no red. Both are the system
 * working: a refusal means a statement was written and stopped before it
 * touched the database, and painting that as a failure teaches people to
 * distrust the times it stays quiet.
 */
function TurnCard({ answer, number }: { answer: AskAnswer; number: number }) {
  const label =
    answer.outcome === "refuse" ? (
      <Badge tone="neutral">Not run</Badge>
    ) : answer.outcome === "clarify" ? (
      <Badge tone="info">Needs one detail</Badge>
    ) : answer.mode === "refine" ? (
      <Badge tone="info">Refines the question above</Badge>
    ) : null;

  return (
    <Card className="rise">
      {/* Numbered, because a session is a sequence and "the one before last"
          is how people refer to an answer they want back. It also says at a
          glance how far in you are, which the scroll position no longer does
          now the table keeps its own. */}
      <CardHeader
        title={
          // Wrapped, not truncated. The heading is the person's own sentence,
          // and a long question ending in an ellipsis is the one thing on this
          // card they cannot look up somewhere else.
          <span className="flex min-w-0 items-start gap-2">
            <span className="tnum mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-soft text-[11px] font-semibold text-brand ring-1 ring-brand/15 ring-inset">
              {number}
            </span>
            <span className="min-w-0 break-words">{answer.question}</span>
          </span>
        }
        actions={label}
      />
      <div className="space-y-4 px-4 py-4 sm:px-5">
        {answer.outcome === "answer" && (
          <>
            {/* The explanation first and largest: it is the answer a person
                reads, and the table underneath is the evidence for it. */}
            <p className="text-[15px] leading-relaxed text-ink">
              {answer.explanation}
            </p>
            <Assumptions items={answer.assumptions} />
            <AnswerView
              columns={answer.columns}
              rows={answer.rows}
              chartHint={answer.chart_hint}
              summary={answer.summary}
            />
          </>
        )}

        {answer.outcome === "refuse" && (
          <div className="flex gap-3">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-ink-soft" />
            <div className="min-w-0 space-y-1.5">
              <p className="text-[15px] leading-relaxed text-ink">{answer.refusal}</p>
              {/* Two different refusals arrive here. One is a statement the
                  guard stopped, and its explanation is worth showing because
                  something was about to run. The other is "this database does
                  not hold that figure" — nothing was written, so the line
                  below would be describing a query that never existed. */}
              {answer.explanation && answer.sql && (
                <p className="text-[13px] leading-relaxed text-ink-soft">
                  What it had been about to run: {answer.explanation}
                </p>
              )}
            </div>
          </div>
        )}

        {answer.outcome === "clarify" && (
          <div className="flex gap-3">
            <Split className="mt-0.5 size-4 shrink-0 text-info" />
            <div className="min-w-0 space-y-1.5">
              <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                A question back
              </p>
              <p className="text-[15px] leading-relaxed text-ink">
                {answer.clarifying_question}
              </p>
              <p className="text-[13px] text-ink-soft">
                Add the missing detail to the question and ask it again. Nothing
                was run, so nothing here is an answer to anything.
              </p>
            </div>
          </div>
        )}

        <QueryDetails answer={answer} />
      </div>
    </Card>
  );
}

/**
 * The line under the box: what is happening, or what is stopping it.
 *
 * While listening it says the one thing that matters — read it before you ask
 * — with the example that makes the point, because "check the transcription"
 * is advice everybody ignores and "a mock sicilian" is not.
 */
function hint(listening: boolean, problem: VoiceProblem | null): string {
  if (listening) {
    return 'Listening. The words land in the box — read them before asking: this hears "Amoxicillin" as "a mock sicilian".';
  }
  if (problem) return problem.message;
  return "Enter asks. Shift and Enter starts a new line.";
}

/** A browser that never had a microphone is a fact, not a warning. */
function hintTone(listening: boolean, problem: VoiceProblem | null): string {
  if (listening) return "text-brand";
  if (problem && problem.kind !== "unsupported") return "text-warn";
  return "text-ink-faint";
}

/**
 * Three questions that fill the box rather than send.
 *
 * The four examples that used to sit here were removed for taking most of a
 * laptop screen to say something a person reads once. These are one compact
 * row and they load the box instead of submitting, so the first thing anyone
 * does on this screen is still read a question before asking it — which is the
 * same rule dictation follows two components down.
 *
 * Each one is a question the benchmark covers, so a demo that starts by
 * clicking one starts on ground that has been checked against hand-written SQL.
 */
const OPENERS = [
  "Which batches expire in the next 90 days?",
  "What is our stock worth at each branch?",
  "Which products are below their reorder point?",
];

/** The three things that happen between pressing Ask and seeing a row. */
const PIPELINE = [
  { icon: Sparkle, text: "Writes one SELECT" },
  { icon: ShieldCheck, text: "Checked, then planned" },
  { icon: Database, text: "Read-only, capped" },
];

/**
 * The screen before anything has been asked.
 *
 * It has to do two jobs at once: fill a large empty area without pretending to
 * be content, and say what this thing is — because "type a question and a
 * language model writes SQL against production" is not an offer anybody should
 * accept without being told the shape of it first. So the guarantees are on
 * the empty screen rather than only in the docs, and they are the same three
 * the API enforces.
 */
function EmptyThread({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-4 text-center">
      {/* A soft brand wash behind the mark. The icon on its own read as a
          missing image rather than an empty state. */}
      <div className="relative mb-5 flex size-14 items-center justify-center">
        <span className="absolute inset-0 rounded-2xl bg-gradient-to-br from-brand-soft to-info-soft" />
        <span className="absolute inset-0 rounded-2xl ring-1 ring-brand/15 ring-inset" />
        <MessagesSquare className="relative size-6 text-brand" strokeWidth={1.5} />
      </div>

      <h2 className="text-[17px] font-semibold tracking-tight text-ink">
        Ask this database a question
      </h2>
      <p className="mt-1.5 max-w-md text-[13px] leading-relaxed text-ink-soft">
        Stock, batches, orders or suppliers — in plain English. Every answer
        arrives with the query that produced it, so it can be checked rather
        than believed.
      </p>

      <ul className="mt-6 flex max-w-xl flex-wrap justify-center gap-2">
        {OPENERS.map((q) => (
          <li key={q}>
            <button
              type="button"
              onClick={() => onPick(q)}
              className={cn(
                "rounded-full border border-line bg-surface px-3 py-1.5",
                "text-[12.5px] text-ink-soft shadow-sm transition-all",
                "hover:-translate-y-px hover:border-brand/40 hover:text-brand hover:shadow",
                "focus-visible:ring-2 focus-visible:ring-brand-ring focus-visible:outline-none",
                "motion-reduce:hover:translate-y-0",
              )}
            >
              {q}
            </button>
          </li>
        ))}
      </ul>

      <div className="mt-8 flex flex-wrap items-center justify-center gap-x-5 gap-y-2">
        {PIPELINE.map(({ icon: Icon, text }, i) => (
          <span key={text} className="flex items-center gap-2">
            {i > 0 && (
              <ChevronRight className="size-3 shrink-0 text-ink-faint/50" aria-hidden />
            )}
            <Icon className="size-3.5 shrink-0 text-brand/70" strokeWidth={1.75} />
            <span className="text-[11.5px] tracking-wide text-ink-faint">{text}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- page */

/**
 * The thread, held outside the component so leaving the screen does not end it.
 *
 * `useState` dies with the route, which meant walking to Stock to check
 * something — the ordinary reason to leave this screen mid-conversation — threw
 * the conversation away. That also made "Start over" a lie: a button that
 * clears the thread implies the thread survives everything else.
 *
 * A module variable rather than `sessionStorage` on purpose. An answer carries
 * up to 200 rows and a session holds many of them, which is a real chance of
 * blowing the 5MB quota — and the failure mode there is a thrown exception on
 * a screen that was working, in exchange for surviving a reload. Leaving the
 * screen is the case that happens; reloading the page is not.
 *
 * Per tab, and gone when the tab is. Nothing here reaches another user.
 */
let retained: { thread: Turn[]; draft: string } = { thread: [], draft: "" };

export function Ask() {
  const [question, setQuestion] = useState(retained.draft);
  const [thread, setThread] = useState<Turn[]>(retained.thread);
  const box = useRef<HTMLTextAreaElement>(null);
  const foot = useRef<HTMLDivElement>(null);

  // Written on every change rather than on unmount: an unmount handler misses
  // a tab being closed, and reading a half-written thread back is worse than
  // reading none.
  useEffect(() => {
    retained = { thread, draft: question };
  }, [thread, question]);

  /**
   * What was already in the box when the microphone was switched on.
   *
   * Dictation is appended to it rather than replacing it, so pressing the
   * button halfway through typing does not eat the half already typed.
   */
  const typed = useRef("");

  const voice = useVoice((heard) => {
    const joined = typed.current ? `${typed.current} ${heard}` : heard;
    setQuestion(joined.slice(0, MAX_QUESTION_CHARS));
  });

  const ask = useMutation({
    mutationFn: (body: AskIn) => api.post<AskAnswer>("/api/v1/ai/ask", body),
    onSuccess: (answer) => {
      setThread((turns) => [...turns, { id: turns.length, answer }]);
      // A question handed back is a question to edit, not one to retype from
      // memory — so the box keeps it and the missing detail gets added to it.
      setQuestion(answer.outcome === "clarify" ? answer.question : "");
      typed.current = "";
    },
  });

  useEffect(() => {
    // A new turn lands below the fold on a laptop, and an answer nobody is
    // looking at reads as nothing having happened.
    if (thread.length > 0) foot.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread.length]);

  useEffect(() => {
    // The box is exactly as tall as its contents, to a ceiling.
    //
    // Driven by the value rather than by `onChange`, because the text changes
    // by four routes and only one of them is a keystroke: dictation writes
    // through `setQuestion`, a clarify puts the question back, sending empties
    // it, and a retained draft arrives already written. Sizing in the change
    // handler grew the box while typing and then left it three lines tall over
    // an empty field after the question was sent.
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_COMPOSER_PX)}px`;
  }, [question]);

  const send = () => {
    const asked = question.trim();
    if (!asked || ask.isPending) return;
    // A microphone still running would type its last words into the box that
    // is about to be emptied, and the next question would start half-written.
    voice.stop();
    ask.mutate({ question: asked, previous: memoryOf(thread) });
  };

  const dictate = () => {
    typed.current = question.trim();
    voice.start();
  };

  const startOver = () => {
    voice.stop();
    ask.reset();
    setThread([]);
    setQuestion("");
    typed.current = "";
  };

  /**
   * An opener loads the box and puts the cursor in it. It does not send.
   *
   * Same rule dictation follows: nothing leaves this screen that the person
   * has not read. It also means an opener is a starting point rather than a
   * fixed demo — the caret is already there to edit "90 days" into "30".
   */
  const pick = (q: string) => {
    setQuestion(q);
    typed.current = q;
    box.current?.focus();
  };

  /** Nothing asked, nothing in flight, nothing broken. */
  const idle = thread.length === 0 && !ask.isPending && !ask.error;

  return (
    /*
     * The page owns its height and does not scroll; the thread inside it does.
     *
     * It was a normal scrolling page with the composer stuck to the bottom,
     * which put the box under the header on an empty screen and moved it down
     * as answers arrived — the one element that must not move was the only one
     * that did. A column of a known height cannot do that: header and composer
     * are fixed rows, the thread is the only thing that grows, and the box is
     * at the bottom from the first paint.
     */
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0">
        <PageHeader
          title="Ask"
          badge={<AiBadge />}
          description="A question in plain English, answered from this database. The query it wrote is under every answer."
          actions={
            thread.length > 0 && (
              <Button size="sm" onClick={startOver}>
                <RotateCcw className="size-3.5" />
                Start over
              </Button>
            )
          }
        />
      </div>

      {/* The only scrolling region on the screen. `pr-1` keeps its scrollbar
          off the cards rather than over them. */}
      <div className="scroll-y min-h-0 flex-1 space-y-4 pr-1">
        {/* Nothing asked yet. */}
        {idle && <EmptyThread onPick={pick} />}

        {/* Transport failures only. A refusal and a question back arrive as
            successful responses, and neither is drawn like this. */}
        {ask.error && <ErrorState error={ask.error} />}

        {thread.map((turn, index) => (
          <TurnCard key={turn.id} answer={turn.answer} number={index + 1} />
        ))}

        {ask.isPending && (
          <Card className="rise">
            <CardHeader
              title={
                <span className="flex min-w-0 items-start gap-2">
                  <span className="tnum mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-soft text-[11px] font-semibold text-brand ring-1 ring-brand/15 ring-inset">
                    {thread.length + 1}
                  </span>
                  <span className="min-w-0 break-words">
                    {ask.variables?.question ?? ""}
                  </span>
                </span>
              }
            />
            {/* The three steps named individually rather than as one
                sentence. A wait nobody can see inside reads as a hang, and
                these are the same three the empty screen promised — so the
                spinner is showing the guarantees being kept. */}
            <div className="space-y-3 px-4 py-5 sm:px-5">
              <div className="flex items-center gap-3">
                <Spinner />
                <p className="text-[13px] text-ink-soft">
                  Writing a query, checking what it is allowed to read, then
                  running it.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pl-8">
                {PIPELINE.map(({ icon: Icon, text }) => (
                  <span key={text} className="flex items-center gap-1.5">
                    <Icon className="size-3 shrink-0 text-brand/60" strokeWidth={1.75} />
                    <span className="text-[11px] text-ink-faint">{text}</span>
                  </span>
                ))}
              </div>
            </div>
          </Card>
        )}

        {/*
          The scroll anchor, and only when there is something to scroll to.
          Rendered unconditionally it was a second child of a `space-y-4`
          parent, so an empty screen carried a `h-full` panel plus 16px of
          margin plus this — sixteen pixels taller than the box that held it,
          which is why a screen with nothing on it still had a scrollbar down
          the side of it.
        */}
        {!idle && <div ref={foot} />}
      </div>

      {/*
        One row: the box, the microphone, the button. It was a three-row
        textarea in a padded card with four example questions under it, which
        is most of a laptop screen given to a control that holds one sentence.
        The box now starts at one line and grows to about six as it is typed
        into, so it is the size of what is in it.
      */}
      <div className="shrink-0 pt-3">
        {/* The ring is on the strip, not the textarea inside it, so the whole
            control lights up as one object — a glowing box inside a still box
            looks like two controls, one of which is broken. */}
        <div
          className={cn(
            "flex items-end gap-2 rounded-xl border border-line bg-surface p-2",
            "shadow-sm transition-all duration-150",
            "hover:border-line-strong",
            "focus-within:border-brand/60 focus-within:shadow-md focus-within:ring-2 focus-within:ring-brand-ring",
          )}
        >
          <Textarea
            ref={box}
            rows={1}
            value={question}
            maxLength={MAX_QUESTION_CHARS}
            aria-label="Your question"
            placeholder="Ask about stock, batches, orders or suppliers…"
            className="min-h-0 resize-none overflow-y-auto border-0 bg-transparent px-2 py-1.5 text-[14px] leading-relaxed shadow-none focus:ring-0"
            onChange={(e) => {
              setQuestion(e.target.value);
              // The next dictation starts from what is in the box now. Not
              // while one is running, though: the base has to stay fixed for
              // the length of an utterance, or each revised interim result
              // gets appended to the one before it.
              if (!voice.listening) typed.current = e.target.value.trim();
            }}
            onKeyDown={(e) => {
              // Enter asks. `isComposing` keeps a transliteration keyboard
              // from submitting the moment it commits a syllable.
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
          />

          {/* No microphone at all where the browser has no speech recognition
              — a button that does nothing when pressed is worse than no
              button, because people keep pressing it. */}
          {voice.supported && (
            <Button
              variant={voice.listening ? "primary" : "secondary"}
              aria-pressed={voice.listening}
              aria-label={voice.listening ? "Stop dictating" : "Dictate the question"}
              className="size-9 shrink-0 p-0"
              onClick={() => (voice.listening ? voice.stop() : dictate())}
            >
              <Mic className={cn("size-4", voice.listening && "animate-pulse")} />
            </Button>
          )}

          <Button
            variant="primary"
            loading={ask.isPending}
            disabled={question.trim().length === 0}
            aria-label="Ask"
            className="size-9 shrink-0 p-0"
            onClick={send}
          >
            <Send className="size-4" />
          </Button>
        </div>

        {/* One line, and only when it has something to say. The keyboard hint
            is not worth a permanent row under the box. */}
        {(voice.listening || voice.problem) && (
          <p className={cn("mt-1.5 px-1 text-[12px]", hintTone(voice.listening, voice.problem))}>
            {hint(voice.listening, voice.problem)}
          </p>
        )}
        {question.length > MAX_QUESTION_CHARS - 100 && (
          <p className="tnum mt-1.5 px-1 text-right text-[12px] text-ink-faint">
            {question.length}/{MAX_QUESTION_CHARS}
          </p>
        )}
      </div>
    </div>
  );
}
