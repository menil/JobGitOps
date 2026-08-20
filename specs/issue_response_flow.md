# JobGitOps Issue Triage & Response Pipeline

When an issue is opened or commented on in your `job-search` repository, the GitHub Actions workflows route the event to either the triage coordinator (`triage.py`) or the conversational responder (`respond.py`). This document explains the full decision logic.

## Pipeline Flowchart

```mermaid
flowchart TD
    Start([GitHub Webhook Event]) --> Trigger{Trigger Event?}
    
    %% Route 1: Triage Pending Label
    Trigger -- Issue Labeled/Opened with 'triage-pending' --> RunTriageScript[triage.py Run]
    RunTriageScript --> TriageCore[Triage & Tailoring Core]
    
    %% Route 2: General Opened or Commented
    Trigger -- Issue Opened or Commented --> RunRespondScript[respond.py Run]
    
    RunRespondScript --> GuardCheck{Passes Safety Guards?\n- Not a bot\n- No status-update marker\n- Not already structured}
    GuardCheck -- No --> ExitSkip([Skip / Terminate])
    
    GuardCheck -- Yes --> EventType{Event Type?}
    
    %% Event Type: Comment
    EventType -- Comment Created --> RunAgentComment[Run Agent Intent Classification]
    RunAgentComment --> ExecActionComment[Execute Status Update, Reply, or Skip]
    
    %% Event Type: Opened
    EventType -- Issue Opened --> BareURLCheck{Is Bare URL Submission?}
    
    BareURLCheck -- Yes --> FetchTriageBare[Fetch Job URL & Run Triage Core\n- already_applied = False]
    FetchTriageBare --> TriageCore
    
    BareURLCheck -- No --> RunAgentOpened[Run Agent Intent Classification]
    RunAgentOpened --> AgentDecision{Agent Action?}
    
    %% Agent Decision: Reply / Skip
    AgentDecision -- Reply / Skip --> ExecReplySkip[Execute Action & Terminate]
    
    %% Agent Decision: Status Update
    AgentDecision -- Status Update --> HasURLStatus{Contains a Job URL?}
    HasURLStatus -- No --> ExecDirectStatus[Apply Status labels & update Projects V2]
    HasURLStatus -- Yes --> AppliedIntent{Status is 'applied'?}
    AppliedIntent -- Yes --> SetAppliedTrue1[Set already_applied = True]
    SetAppliedTrue1 --> FetchTriageOpened
    AppliedIntent -- No --> ExecDirectStatus
    
    %% Agent Decision: Triage
    AgentDecision -- Triage --> HasAppliedKeywords{Title/Body contains 'applied' keywords?\n- applied, interview, loop, screen, offer}
    HasAppliedKeywords -- Yes --> SetAppliedTrue2[Set already_applied = True]
    HasAppliedKeywords -- No --> SetAppliedFalse[Set already_applied = False]
    
    SetAppliedTrue2 --> FetchTriageOpened
    SetAppliedFalse --> FetchTriageOpened
    
    FetchTriageOpened[Fetch Job URL, Infer Details, and Build Canonical Body] --> TriageCore
    
    %% Triage Core Details
    subgraph Triage Core
        TriageCore --> EvaluateFit[LLM Evaluates Fit Score]
        EvaluateFit --> TitleRename["Update Issue Title to [Company] Role"]
        TitleRename --> CheckAlreadyApplied{already_applied == True?}
        
        %% Path A: already_applied is True
        CheckAlreadyApplied -- Yes --> TailorResumeTrue[Tailor Resume & Generate PDF]
        TailorResumeTrue --> CreateBranchTrue[Create & Push Application Branch]
        CreateBranchTrue --> LabelApplied[Apply label 'applied'\nUpdate Projects V2 to 'Applied'\nPost 'Already Applied' Triage Comment]
        
        %% Path B: already_applied is False
        CheckAlreadyApplied -- No --> CompareThreshold{Fit Score >= Threshold?}
        CompareThreshold -- No (Mismatch) --> LabelMismatch[Apply Mismatch labels\nClose Issue\nPost Mismatch Comment]
        CompareThreshold -- Yes (Match) --> TailorResumeFalse[Tailor Resume & Generate PDF]
        TailorResumeFalse --> CreateBranchFalse[Create & Push Application Branch]
        CreateBranchFalse --> LabelReady[Apply fit grade & 'ready-to-apply'\nUpdate Projects V2 to 'Ready to Apply'\nPost 'Match Approved' Triage Comment]
    end
```

---

## Detailed Logic Breakdown

### 1. The Entry Points
* **`triage.py` (Triage Workflow):** Triggers directly when an issue is opened or labeled with `triage-pending`. It assumes the issue contains job description details (usually structured) and immediately evaluates it.
* **`respond.py` (Response Workflow):** Triggers on any general issue opening or comment creation. It performs safety checks (ignoring bots and recursive loops) and determines the user's intent.

### 2. Intent Detection for Opened Issues
When a user opens an issue:
* **Bare URL Check:** If the issue is simply a link to a job posting (with no additional context), intent classification is bypassed. It proceeds directly to triage the URL.
* **Agent Classification:** If there is text alongside the URL, `run_agent` determines the action:
  * **Triage:** The LLM indicates the user wants to evaluate a job description.
  * **Status Update:** The LLM indicates the user is updating the status of a job (e.g. reporting they applied).
  * **Reply/Skip:** General conversation or actions that require no status/triage changes.

### 3. Intercepting the "Already Applied" Intent
If the agent detects a `status_update` to `applied` **and** a job URL is present, or if it classifies it as `triage` but the issue contains status keywords (like `"applied"`, `"interview"`, etc.):
* The pipeline flags the process with `already_applied = True`.
* Instead of running a simple status change or a standard triage, the bot fetches the URL, infers the job details, renames the issue title to `[Company] Role`, and generates the tailored resume.

### 4. Triage & Tailoring Core
During triage:
* **Mismatch Bypass:** If `already_applied` is `True`, the fit score threshold check is completely bypassed. Even if the score is low, the issue is kept open.
* **Badges and Statuses:** 
  * If `already_applied` is `True`, the issue is tagged as `applied` (moving to the **Applied** column in your Projects board), and the fit grade labels are skipped.
  * If `already_applied` is `False`, the issue is evaluated against the threshold. Approved matches get the fit grade label (e.g. `fit:B`) and the `ready-to-apply` label (moving to the **Ready to Apply** board column). Mismatches are closed.
