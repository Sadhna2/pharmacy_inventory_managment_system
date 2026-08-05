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
import { ChevronRight, Mic, RotateCcw, Send, ShieldAlert, Split } from "lucide-react";
import { api } from "@/lib/api";
import { cn, qty } from "@/lib/format";
import { useVoice, type VoiceProblem } from "@/lib/useVoice";
import { PageHeader } from "@/components/Shell";
import AnswerView, { type ChartHint } from "@/components/AnswerView";
import {
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
 * The questions a beginner does not know they are allowed to ask.
 *
 * Real ones off this shop floor rather than "show me the data" — each names
 * something this schema actually holds, so a first click comes back with rows
 * instead of a clarifying question. Nobody reads documentation for a text box;
 * they copy the example nearest to what they wanted.
 */
const STARTERS = [
  "Which batches expire in the next 60 days, and what are they worth?",
  "What is my stock worth at each branch?",
  "What was written off last month, and why?",
  "Which purchase orders are overdue?",
];

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

function SqlBlock({ label, sql }: { label?: string; sql: string }) {
  return (
    <div className="min-w-0">
      {label && (
        <p className="mb-1 text-[11px] tracking-wide text-ink-faint uppercase">
          {label}
        </p>
      )}
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
          "flex cursor-pointer items-center gap-2 px-3 py-2",
          "text-[13px] text-ink-soft select-none",
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
function Assumptions({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5">
      <p className="text-[11px] tracking-wide text-ink-faint uppercase">
        What it decided for you
      </p>
      <ul className="mt-1.5 space-y-1">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-[13px] text-ink-soft">
            <span className="mt-[7px] size-1 shrink-0 rounded-full bg-ink-faint" />
            {item}
          </li>
        ))}
      </ul>
    </div>
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
function TurnCard({ answer }: { answer: AskAnswer }) {
  const label =
    answer.outcome === "refuse" ? (
      <Badge tone="neutral">Not run</Badge>
    ) : answer.outcome === "clarify" ? (
      <Badge tone="info">Needs one detail</Badge>
    ) : answer.mode === "refine" ? (
      <Badge tone="info">Refines the question above</Badge>
    ) : null;

  return (
    <Card>
      <CardHeader title={answer.question} actions={label} />
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
              {answer.explanation && (
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

/* --------------------------------------------------------------------- page */

export function Ask() {
  const [question, setQuestion] = useState("");
  const [thread, setThread] = useState<Turn[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);
  const foot = useRef<HTMLDivElement>(null);

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

  const send = () => {
    const asked = question.trim();
    if (!asked || ask.isPending) return;
    // A microphone still running would type its last words into the box that
    // is about to be emptied, and the next question would start half-written.
    voice.stop();
    ask.mutate({ question: asked, previous: memoryOf(thread) });
  };

  const fill = (starter: string) => {
    setQuestion(starter);
    typed.current = starter;
    box.current?.focus();
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

  return (
    <>
      <PageHeader
        title="Ask"
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

      <div className="space-y-4">
        <Card className="p-4 sm:p-5">
          <div className="flex items-start gap-2">
            <Textarea
              ref={box}
              rows={3}
              value={question}
              maxLength={MAX_QUESTION_CHARS}
              aria-label="Your question"
              placeholder="Which batches expire before the end of next month?"
              className="min-h-24 text-[15px] leading-relaxed"
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
            {/* No microphone at all where the browser has no speech
                recognition — a button that does nothing when pressed is worse
                than no button, because people keep pressing it. */}
            {voice.supported && (
              <Button
                variant={voice.listening ? "primary" : "secondary"}
                aria-pressed={voice.listening}
                aria-label={
                  voice.listening ? "Stop dictating" : "Dictate the question"
                }
                className="shrink-0 px-3"
                onClick={() => (voice.listening ? voice.stop() : dictate())}
              >
                <Mic className={cn("size-4", voice.listening && "animate-pulse")} />
              </Button>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <p className={cn("text-[12px]", hintTone(voice.listening, voice.problem))}>
              {hint(voice.listening, voice.problem)}
            </p>
            <div className="ml-auto flex items-center gap-3">
              {/* Only near the ceiling. A counter on every question implies a
                  budget nobody has to think about at forty words. */}
              {question.length > MAX_QUESTION_CHARS - 100 && (
                <span className="tnum text-[12px] text-ink-faint">
                  {question.length}/{MAX_QUESTION_CHARS}
                </span>
              )}
              <Button
                variant="primary"
                loading={ask.isPending}
                disabled={question.trim().length === 0}
                onClick={send}
              >
                <Send className="size-4" />
                Ask
              </Button>
            </div>
          </div>

          {thread.length === 0 && !ask.isPending && (
            <div className="mt-4 border-t border-line pt-4">
              <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                Things people ask
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                {STARTERS.map((starter) => (
                  <Button
                    key={starter}
                    size="sm"
                    className="h-auto max-w-full py-1.5 text-left whitespace-normal"
                    onClick={() => fill(starter)}
                  >
                    {starter}
                  </Button>
                ))}
              </div>
            </div>
          )}
        </Card>

        {/* Transport failures only. A refusal and a question back arrive as
            successful responses, and neither is drawn like this. */}
        {ask.error && <ErrorState error={ask.error} />}

        {thread.map((turn) => (
          <TurnCard key={turn.id} answer={turn.answer} />
        ))}

        {ask.isPending && (
          <Card>
            <CardHeader title={ask.variables?.question ?? ""} />
            <div className="flex items-center gap-3 px-4 py-5 sm:px-5">
              <Spinner />
              <p className="text-[13px] text-ink-soft">
                Writing a query, checking what it is allowed to read, then
                running it.
              </p>
            </div>
          </Card>
        )}

        <div ref={foot} />
      </div>
    </>
  );
}
