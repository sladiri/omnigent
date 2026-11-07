import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";
import { COMPOSER_CONTENT_INSET_CLASS } from "@/components/composer/ChatComposer";

interface PreparedDraft {
  token: string;
  workflow: string;
  repository: string;
  task: string;
  baseline: string;
  base: string;
  branch: string | null;
  publish: boolean;
}

export function WorkflowStartButton({
  sessionId,
  disabled,
  onStart,
}: {
  sessionId: string;
  disabled: boolean;
  onStart: (command: string) => void;
}) {
  const { data, isError } = useQuery<{
    draft: PreparedDraft | null;
    question?: { token: string; text: string } | null;
  }>({
    queryKey: ["workflow-draft", sessionId],
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/workflow-draft`,
      );
      if (!response.ok) throw new Error("Could not load prepared workflow");
      return response.json();
    },
    refetchInterval: 2000,
  });
  if (isError)
    return (
      <p role="status" className={COMPOSER_CONTENT_INSET_CLASS}>
        Could not load workflow actions. Reconnecting…
      </p>
    );
  if (data?.question)
    return (
      <WorkflowAnswer
        key={`${sessionId}:${data.question.token}`}
        question={data.question}
        disabled={disabled}
        onAnswer={onStart}
      />
    );
  const draft = data?.draft;
  if (!draft) return null;
  return (
    <div className={`${COMPOSER_CONTENT_INSET_CLASS} space-y-2 pb-2`}>
      <details className="text-xs break-words">
        <summary>Review prepared workflow</summary>
        <p>
          {draft.workflow} · {draft.repository}
        </p>
        <p>
          Baseline: {draft.baseline} · Target: {draft.base}
        </p>
        <p>
          Branch: {draft.branch ?? "Assigned by workflow"} · Publication:{" "}
          {draft.publish ? "enabled" : "disabled"}
        </p>
        <p className="whitespace-pre-wrap">{draft.task}</p>
      </details>
      <Button
        type="button"
        disabled={disabled}
        onClick={() => onStart(`/workflow start-draft ${draft.token}`)}
      >
        Start workflow
      </Button>
    </div>
  );
}

function WorkflowAnswer({
  question,
  disabled,
  onAnswer,
}: {
  question: { token: string; text: string };
  disabled: boolean;
  onAnswer: (command: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [answer, setAnswer] = useState("");
  return (
    <div className={`${COMPOSER_CONTENT_INSET_CLASS} space-y-2 pb-2`}>
      <p className="whitespace-pre-wrap text-sm">{question.text}</p>
      {open ? (
        <>
          <label className="text-sm">
            Your workflow answer
            <Textarea
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              disabled={disabled}
              autoFocus
            />
          </label>
          <Button
            type="button"
            disabled={disabled || !answer.trim()}
            onClick={() => onAnswer(`/workflow answer-question ${question.token} ${answer.trim()}`)}
          >
            Submit answer
          </Button>
          <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </>
      ) : (
        <Button type="button" disabled={disabled} onClick={() => setOpen(true)}>
          Answer
        </Button>
      )}
    </div>
  );
}
