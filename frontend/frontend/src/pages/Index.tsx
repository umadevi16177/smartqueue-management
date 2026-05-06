import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Clock,
  Droplet,
  HeartPulse,
  Minus,
  Pause,
  Play,
  Plus,
  Radio,
  RefreshCw,
  ScanLine,
  Stethoscope,
  Users,
  Waves,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { api, type ActiveJourney, type BackendDepartment, type DepartmentPatch, type UnclaimedPatient } from "@/lib/api";
import { UserPlus } from "lucide-react";

type Status = "active" | "busy" | "maintenance" | "closed";

type Department = {
  id: string;
  code: string;
  name: string;
  icon: LucideIcon;
  queue: number;
  waitMin: number;
  serving: string;
  status: Status;
  capacity: number;
};

const DEPT_META: Record<
  string,
  { id: string; name: string; icon: LucideIcon; capacity: number }
> = {
  BLOOD: { id: "blood", name: "Blood Test", icon: Droplet, capacity: 20 },
  ECG: { id: "ecg", name: "ECG", icon: HeartPulse, capacity: 15 },
  ULTRASOUND: { id: "ultrasound", name: "Ultrasound", icon: Waves, capacity: 20 },
  XRAY: { id: "xray", name: "X-Ray", icon: ScanLine, capacity: 18 },
};

const statusMeta: Record<
  Status,
  { label: string; chip: string; dot: string; ring: string }
> = {
  active: {
    label: "Active",
    chip: "bg-status-active/15 text-status-active border-status-active/30",
    dot: "bg-status-active",
    ring: "ring-status-active/30",
  },
  busy: {
    label: "Busy",
    chip: "bg-status-busy/15 text-status-busy border-status-busy/30",
    dot: "bg-status-busy",
    ring: "ring-status-busy/30",
  },
  maintenance: {
    label: "Maintenance",
    chip: "bg-status-issue/15 text-status-issue border-status-issue/30",
    dot: "bg-status-issue",
    ring: "ring-status-issue/30",
  },
  closed: {
    label: "Closed",
    chip: "bg-status-closed/15 text-status-closed border-status-closed/30",
    dot: "bg-status-closed",
    ring: "ring-status-closed/30",
  },
};

function backendToDept(b: BackendDepartment): Department {
  const meta = DEPT_META[b.code] ?? {
    id: b.code.toLowerCase(),
    name: b.code,
    icon: Activity,
    capacity: 20,
  };
  let status: Status;
  if (b.availability === "closed") status = "closed";
  else if (b.availability === "maintenance") status = "maintenance";
  else status = b.queue_length >= meta.capacity * 0.8 ? "busy" : "active";
  return {
    id: meta.id,
    code: b.code,
    name: meta.name,
    icon: meta.icon,
    queue: b.queue_length,
    waitMin: b.estimated_wait_minutes,
    serving: "—",
    status,
    capacity: meta.capacity,
  };
}

const Index = () => {
  const qc = useQueryClient();
  const { data, isLoading, isError, error, dataUpdatedAt, refetch } = useQuery({
    queryKey: ["departments"],
    queryFn: api.listDepartments,
    refetchInterval: 5000,
  });

  const departments: Department[] = useMemo(
    () => (data ?? []).map(backendToDept),
    [data]
  );

  const { data: activeJourneys } = useQuery({
    queryKey: ["active-journeys"],
    queryFn: api.listActiveJourneys,
    refetchInterval: 5000,
  });

  const { data: unclaimed } = useQuery({
    queryKey: ["unclaimed-patients"],
    queryFn: api.listUnclaimedPatients,
    refetchInterval: 5000,
  });

  const [registerName, setRegisterName] = useState("");
  const [registerPid, setRegisterPid] = useState("");
  const [lastIssued, setLastIssued] = useState<{
    id: string;
    seq: number;
    name: string;
    reused: boolean;
  } | null>(null);

  const registerMut = useMutation({
    mutationFn: ({ name, pid }: { name: string; pid?: string }) =>
      api.registerPatient(name, pid),
    onSuccess: (data, variables) => {
      const reused = !!variables.pid;
      setLastIssued({
        id: data.patient_id,
        seq: data.sequence_number,
        name: data.display_name,
        reused,
      });
      setRegisterName("");
      setRegisterPid("");
      qc.invalidateQueries({ queryKey: ["unclaimed-patients"] });
      qc.invalidateQueries({ queryKey: ["active-journeys"] });
      toast({
        title: `Queue #${data.sequence_number} · ${data.patient_id}`,
        description: `${data.display_name}${reused ? " (returning)" : ""}`,
      });
    },
    onError: (e: Error) =>
      toast({ title: "Registration failed", description: e.message }),
  });

  const submitRegister = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = registerName.trim();
    const pid = registerPid.trim().toUpperCase();
    if (trimmed) registerMut.mutate({ name: trimmed, pid: pid || undefined });
  };

  const patchMut = useMutation({
    mutationFn: ({ code, patch }: { code: string; patch: DepartmentPatch }) =>
      api.patchDepartment(code, patch),
    onSuccess: (updated) => {
      qc.setQueryData<BackendDepartment[] | undefined>(["departments"], (prev) =>
        prev
          ? prev.map((d) => (d.code === updated.code ? updated : d))
          : [updated]
      );
    },
    onError: (e: Error) => {
      toast({ title: "Update failed", description: e.message });
    },
  });

  const updateDept = (
    code: string,
    patch: DepartmentPatch,
    successMessage?: string
  ) =>
    patchMut.mutate(
      { code, patch },
      {
        onSuccess: () => {
          if (successMessage) toast({ title: successMessage });
        },
      }
    );

  // FCFS-sorted patient queue per department. Patients are routed to a
  // department based on their `current_test`; the list shown on each
  // department card is exactly these journeys, sorted by sequence_number.
  const queueByDept = useMemo(() => {
    const m = new Map<string, ActiveJourney[]>();
    (activeJourneys ?? [])
      .slice()
      .sort(
        (a, b) =>
          (a.sequence_number ?? Number.MAX_SAFE_INTEGER) -
          (b.sequence_number ?? Number.MAX_SAFE_INTEGER)
      )
      .forEach((j) => {
        if (j.current_test && j.status !== "done") {
          const list = m.get(j.current_test) ?? [];
          list.push(j);
          m.set(j.current_test, list);
        }
      });
    return m;
  }, [activeJourneys]);

  // IDs of journeys we've just clicked-to-complete — used to fade-out the
  // row before the refetch arrives, so the staff member sees instant
  // feedback without waiting on the round-trip.
  const [completingIds, setCompletingIds] = useState<Set<number>>(new Set());

  const completeStepMut = useMutation({
    mutationFn: (journeyId: number) => api.completeCurrentStep(journeyId),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["active-journeys"] });
      qc.invalidateQueries({ queryKey: ["departments"] });
      toast({
        title: "Marked completed",
        description:
          data.completed_test
            ? `${data.completed_test}${data.journey.current_test ? ` → next: ${data.journey.current_test}` : " — journey complete"}`
            : "Step completed",
      });
    },
    onError: (e: Error, journeyId) => {
      // Revert the optimistic fade so the staff can see the row again.
      setCompletingIds((s) => {
        const next = new Set(s);
        next.delete(journeyId);
        return next;
      });
      toast({ title: "Failed to mark completed", description: e.message });
    },
    onSettled: (_data, _err, journeyId) => {
      // After the refetch lands, the row will be gone from the queue
      // anyway; clear the fading-id set on the next refetch tick.
      setTimeout(
        () =>
          setCompletingIds((s) => {
            const next = new Set(s);
            next.delete(journeyId);
            return next;
          }),
        600
      );
    },
  });

  const completeStep = (journeyId: number) => {
    if (completingIds.has(journeyId)) return; // double-click guard
    setCompletingIds((s) => new Set(s).add(journeyId));
    completeStepMut.mutate(journeyId);
  };

  const totalPatients = useMemo(
    () => departments.reduce((s, d) => s + d.queue, 0),
    [departments]
  );
  const avgWait = useMemo(() => {
    const open = departments.filter((d) => d.status === "active" || d.status === "busy");
    if (!open.length) return 0;
    return Math.round(open.reduce((s, d) => s + d.waitMin, 0) / open.length);
  }, [departments]);
  const delayedCount = departments.filter(
    (d) => d.waitMin >= 30 && d.status !== "closed" && d.status !== "maintenance"
  ).length;
  const alerts = departments.filter(
    (d) =>
      d.status === "maintenance" ||
      d.status === "closed" ||
      d.queue >= d.capacity * 0.85 ||
      d.waitMin >= 30
  );

  const secondsAgo = Math.max(0, Math.floor((Date.now() - dataUpdatedAt) / 1000));

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-30 border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-primary/15 text-primary ring-1 ring-primary/30">
              <Stethoscope className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-semibold tracking-tight">
                Smart Queue · Control Center
              </h1>
              <p className="text-xs text-muted-foreground">
                Connected to FastAPI backend · /api/departments
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className="hidden items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs sm:flex">
              <span className="relative flex h-2.5 w-2.5">
                <span
                  className={cn(
                    "absolute inline-flex h-2.5 w-2.5 rounded-full",
                    isError ? "bg-status-issue" : "pulse-dot bg-status-active"
                  )}
                />
                <span
                  className={cn(
                    "relative inline-flex h-2.5 w-2.5 rounded-full",
                    isError ? "bg-status-issue" : "bg-status-active"
                  )}
                />
              </span>
              <span
                className={cn(
                  "font-medium",
                  isError ? "text-status-issue" : "text-status-active"
                )}
              >
                {isError ? "Disconnected" : "Live"}
              </span>
              <span className="text-muted-foreground">
                · {isError ? "backend unreachable" : `updated ${secondsAgo}s ago`}
              </span>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                refetch();
                toast({ title: "Re-fetched from queue server" });
              }}
              className="gap-2"
            >
              <RefreshCw className="h-4 w-4" />
              Sync
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1500px] px-6 py-6">
        {isError && (
          <div className="mb-6 rounded-2xl border border-status-issue/40 bg-status-issue/10 p-4 text-sm">
            <p className="font-medium text-status-issue">Backend unreachable</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {(error as Error)?.message ??
                "Unable to reach /api/departments. Make sure the FastAPI server is running on port 8000."}
            </p>
          </div>
        )}

        <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="Patients in system"
            value={totalPatients.toString()}
            sub="Across all departments"
            icon={Users}
            tone="info"
          />
          <StatCard
            label="Average wait time"
            value={`${avgWait} min`}
            sub="Open departments only"
            icon={Clock}
            tone={avgWait >= 30 ? "busy" : "active"}
          />
          <StatCard
            label="Delayed departments"
            value={delayedCount.toString()}
            sub="Wait ≥ 30 min"
            icon={Activity}
            tone={delayedCount > 0 ? "busy" : "active"}
          />
          <StatCard
            label="Active alerts"
            value={alerts.length.toString()}
            sub={alerts.length ? "Needs attention" : "All systems normal"}
            icon={AlertTriangle}
            tone={alerts.length ? "issue" : "active"}
          />
        </section>

        <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-[1fr_360px]">
          <section>
            <SectionHeader
              title="Departments"
              caption={
                isLoading
                  ? "Loading…"
                  : "Real-time queue control · click any action to update"
              }
            />
            {departments.length === 0 && !isLoading ? (
              <div className="card-elevated rounded-2xl border border-border p-8 text-center text-sm text-muted-foreground">
                No departments returned by the backend.
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
                {departments.map((d) => (
                  <DepartmentCard
                    key={d.code}
                    dept={d}
                    queue={queueByDept.get(d.code) ?? []}
                    completingIds={completingIds}
                    onComplete={completeStep}
                    onUpdate={updateDept}
                  />
                ))}
              </div>
            )}
          </section>

          <aside>
            <SectionHeader
              title="Reception desk"
              caption="New walk-in or returning patient — queue # is per visit"
            />
            <div className="card-elevated mb-4 rounded-2xl border border-border p-4">
              <form onSubmit={submitRegister} className="flex flex-col gap-3">
                <div>
                  <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    Patient name
                  </label>
                  <Input
                    value={registerName}
                    onChange={(e) => setRegisterName(e.target.value)}
                    placeholder="e.g. Anjali Devi"
                    className="mt-1 h-9"
                    disabled={registerMut.isPending}
                  />
                </div>
                <div>
                  <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    Existing Patient ID{" "}
                    <span className="font-normal normal-case text-muted-foreground/70">
                      (optional — leave blank for new)
                    </span>
                  </label>
                  <Input
                    value={registerPid}
                    onChange={(e) => setRegisterPid(e.target.value.toUpperCase())}
                    placeholder="P-001"
                    className="mt-1 h-9 font-mono"
                    disabled={registerMut.isPending}
                  />
                </div>
                <Button
                  type="submit"
                  className="gap-2"
                  disabled={!registerName.trim() || registerMut.isPending}
                >
                  <UserPlus className="h-4 w-4" />
                  {registerMut.isPending
                    ? "Assigning queue #…"
                    : registerPid.trim()
                    ? "Reuse ID & assign queue #"
                    : "Register & issue new ID"}
                </Button>
              </form>
              {lastIssued && (
                <div className="mt-3 rounded-xl bg-primary/10 p-4 text-center ring-1 ring-primary/30">
                  <div className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
                    Queue Number
                  </div>
                  <div
                    className="mt-1 font-mono text-6xl font-extrabold leading-none text-primary tabular-nums"
                    aria-label={`Queue number ${lastIssued.seq}`}
                  >
                    #{lastIssued.seq}
                  </div>
                  <div className="mt-3 text-sm font-medium">{lastIssued.name}</div>
                  <div className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-background/60 px-2.5 py-1 text-[10px]">
                    <span className="uppercase tracking-wider text-muted-foreground">
                      Permanent ID
                    </span>
                    <span className="font-mono font-semibold">{lastIssued.id}</span>
                    {lastIssued.reused && (
                      <span className="rounded-sm bg-status-active/15 px-1.5 text-[9px] font-medium text-status-active">
                        REUSED
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>

            {unclaimed && unclaimed.length > 0 && (
              <div className="card-elevated mb-4 rounded-2xl border border-amber-500/40 bg-amber-500/5 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-semibold text-amber-500">
                    Awaiting Telegram
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {unclaimed.length} pending
                  </span>
                </div>
                <ul className="space-y-2">
                  {unclaimed
                    .slice()
                    .sort(
                      (a, b) =>
                        (a.sequence_number ?? Number.MAX_SAFE_INTEGER) -
                        (b.sequence_number ?? Number.MAX_SAFE_INTEGER)
                    )
                    .slice(0, 8)
                    .map((p: UnclaimedPatient) => (
                      <li
                        key={p.id}
                        className="flex items-center gap-3 rounded-lg bg-background/60 p-2"
                      >
                        <div className="flex flex-col items-center justify-center rounded-md bg-primary/15 px-2 py-1 leading-none text-primary">
                          <span className="text-[8px] font-semibold uppercase tracking-wider text-muted-foreground">
                            Queue
                          </span>
                          <span className="font-mono text-base font-bold tabular-nums">
                            #{p.sequence_number ?? "—"}
                          </span>
                        </div>
                        <div className="min-w-0 flex-1 leading-tight">
                          <div className="truncate text-sm font-medium">
                            {p.display_name ?? "—"}
                          </div>
                          <div className="text-[10px] text-muted-foreground">
                            <span className="uppercase tracking-wider">ID</span>{" "}
                            <span className="font-mono font-semibold text-foreground/80">
                              {p.patient_identifier}
                            </span>
                          </div>
                        </div>
                        <span className="shrink-0 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[9px] font-medium uppercase tracking-wider text-amber-600 dark:text-amber-400">
                          Awaiting Telegram
                        </span>
                      </li>
                    ))}
                </ul>
              </div>
            )}

            <SectionHeader
              title="Active patients"
              caption={`${(activeJourneys ?? []).filter(j => j.status !== 'done').length} in progress`}
            />
            <div className="card-elevated mb-4 rounded-2xl border border-border p-2">
              {!activeJourneys || activeJourneys.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                  <p className="text-sm font-medium">No registered patients yet</p>
                  <p className="text-xs text-muted-foreground">
                    Patients register via @Smart_queue_patient_bot on Telegram.
                  </p>
                </div>
              ) : (
                <ul className="divide-y divide-border/60">
                  {activeJourneys
                    .slice()
                    .sort(
                      (a, b) =>
                        (a.sequence_number ?? Number.MAX_SAFE_INTEGER) -
                        (b.sequence_number ?? Number.MAX_SAFE_INTEGER)
                    )
                    .slice(0, 8)
                    .map((j) => (
                      <li key={j.journey_id} className="p-3">
                        {/* Queue # dominates; permanent Patient ID is secondary. */}
                        <div className="flex items-start gap-3">
                          <div
                            title="FCFS queue number — visit order"
                            className="flex shrink-0 flex-col items-center rounded-lg bg-primary/15 px-2.5 py-1.5 leading-none text-primary ring-1 ring-primary/25"
                          >
                            <span className="text-[8px] font-semibold uppercase tracking-widest text-muted-foreground">
                              Queue
                            </span>
                            <span className="mt-0.5 font-mono text-2xl font-extrabold tabular-nums">
                              #{j.sequence_number ?? "—"}
                            </span>
                          </div>
                          <div className="min-w-0 flex-1 leading-tight">
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate text-sm font-semibold">
                                {j.display_name ?? `chat-${j.telegram_chat_id}`}
                              </span>
                              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                                {j.status}
                              </span>
                            </div>
                            <div className="mt-0.5 text-[10px] text-muted-foreground">
                              <span className="uppercase tracking-wider">
                                Permanent ID
                              </span>{" "}
                              <span className="font-mono font-semibold text-foreground/80">
                                {j.patient_identifier ?? "—"}
                              </span>
                              <span className="ml-2">· {j.language.toUpperCase()}</span>
                            </div>
                            <div className="mt-1 text-xs text-muted-foreground">
                              {j.current_test ? (
                                <>
                                  now:{" "}
                                  <span className="font-medium text-foreground">
                                    {j.current_test}
                                  </span>
                                  {j.current_token && (
                                    <span className="ml-2 font-mono text-[11px]">
                                      [{j.current_token}]
                                    </span>
                                  )}
                                </>
                              ) : (
                                <>journey complete</>
                              )}
                            </div>
                          </div>
                        </div>
                        <div className="mt-2 flex gap-1">
                          {j.steps.map((s) => (
                            <span
                              key={s.step_index}
                              title={`${s.test_code} · ${s.department_status}`}
                              className={cn(
                                "h-1.5 flex-1 rounded-full",
                                s.department_status === "completed"
                                  ? "bg-status-active"
                                  : s.department_status === "in_queue"
                                  ? "bg-status-busy"
                                  : s.department_status === "reserved"
                                  ? "bg-status-issue"
                                  : "bg-secondary"
                              )}
                            />
                          ))}
                        </div>
                      </li>
                    ))}
                </ul>
              )}
            </div>
            <SectionHeader title="Alerts & exceptions" caption="Live issue feed" />
            <div className="card-elevated rounded-2xl border border-border p-2">
              {alerts.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
                  <div className="grid h-12 w-12 place-items-center rounded-full bg-status-active/15 text-status-active">
                    <Activity className="h-5 w-5" />
                  </div>
                  <p className="text-sm font-medium">All systems normal</p>
                  <p className="text-xs text-muted-foreground">
                    No overloaded or closed departments.
                  </p>
                </div>
              ) : (
                <ul className="divide-y divide-border/60">
                  {alerts.map((a) => {
                    const reason =
                      a.status === "maintenance"
                        ? "Under maintenance"
                        : a.status === "closed"
                        ? "Department closed"
                        : a.queue >= a.capacity * 0.85
                        ? "Queue overloaded"
                        : "Long wait time";
                    const tone: Status =
                      a.status === "maintenance" || a.status === "closed"
                        ? "maintenance"
                        : "busy";
                    return (
                      <li key={a.code} className="flex items-start gap-3 p-3">
                        <span
                          className={cn(
                            "mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full",
                            statusMeta[tone].dot
                          )}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2">
                            <p className="truncate text-sm font-medium">{a.name}</p>
                            <span className="text-[11px] text-muted-foreground">
                              just now
                            </span>
                          </div>
                          <p className="text-xs text-muted-foreground">
                            {reason} · queue {a.queue}/{a.capacity} · {a.waitMin} min
                          </p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>

            <div className="mt-4 card-elevated rounded-2xl border border-border p-4">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Radio className="h-4 w-4 text-primary" />
                Telegram bot bridge
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Patients receive automatic position & ETA updates whenever you change
                queue data here.
              </p>
              <div className="mt-3 flex items-center justify-between rounded-lg bg-secondary/60 px-3 py-2 text-xs">
                <span className="text-muted-foreground">Backend</span>
                <span className="font-mono text-[11px]">/api/departments</span>
              </div>
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
};

const SectionHeader = ({
  title,
  caption,
}: {
  title: string;
  caption?: string;
}) => (
  <div className="mb-3 flex items-end justify-between">
    <div>
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      {caption && <p className="text-xs text-muted-foreground">{caption}</p>}
    </div>
  </div>
);

type Tone = "active" | "busy" | "issue" | "info";
const toneClasses: Record<Tone, { bg: string; text: string }> = {
  active: { bg: "bg-status-active/15", text: "text-status-active" },
  busy: { bg: "bg-status-busy/15", text: "text-status-busy" },
  issue: { bg: "bg-status-issue/15", text: "text-status-issue" },
  info: { bg: "bg-status-info/15", text: "text-status-info" },
};

const StatCard = ({
  label,
  value,
  sub,
  icon: Icon,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  icon: LucideIcon;
  tone: Tone;
}) => (
  <div className="card-elevated animate-fade-in rounded-2xl border border-border p-5">
    <div className="flex items-start justify-between">
      <div>
        <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
        <p className="mt-2 text-3xl font-semibold tracking-tight">{value}</p>
        <p className="mt-1 text-xs text-muted-foreground">{sub}</p>
      </div>
      <div
        className={cn(
          "grid h-10 w-10 place-items-center rounded-xl",
          toneClasses[tone].bg,
          toneClasses[tone].text
        )}
      >
        <Icon className="h-5 w-5" />
      </div>
    </div>
  </div>
);

const DepartmentCard = ({
  dept,
  queue,
  completingIds,
  onComplete,
  onUpdate,
}: {
  dept: Department;
  queue: ActiveJourney[];
  completingIds: Set<number>;
  onComplete: (journeyId: number) => void;
  onUpdate: (code: string, patch: DepartmentPatch, message?: string) => void;
}) => {
  const Icon = dept.icon;
  const meta = statusMeta[dept.status];
  const [waitDraft, setWaitDraft] = useState<string>(String(dept.waitMin));
  const isOpen = dept.status === "active" || dept.status === "busy";
  const fillPct = Math.min(100, Math.round((dept.queue / dept.capacity) * 100));

  const addPatient = () => {
    if (!isOpen) return toast({ title: "Department is unavailable" });
    onUpdate(
      dept.code,
      {
        queue_length: dept.queue + 1,
        estimated_wait_minutes: dept.waitMin + 2,
      },
      `${dept.name}: patient added`
    );
  };

  const removePatient = () => {
    if (dept.queue === 0) return;
    onUpdate(
      dept.code,
      {
        queue_length: Math.max(0, dept.queue - 1),
        estimated_wait_minutes: Math.max(0, dept.waitMin - 2),
      },
      `${dept.name}: patient served`
    );
  };

  const commitWait = () => {
    const n = Math.max(0, Math.min(240, parseInt(waitDraft || "0", 10) || 0));
    setWaitDraft(String(n));
    if (n !== dept.waitMin) {
      onUpdate(
        dept.code,
        { estimated_wait_minutes: n },
        `${dept.name}: wait time updated`
      );
    }
  };

  const setMaintenance = () =>
    onUpdate(
      dept.code,
      { availability: "maintenance" },
      `${dept.name}: marked as maintenance`
    );
  const resume = () =>
    onUpdate(dept.code, { availability: "open" }, `${dept.name}: resumed operations`);
  const close = () =>
    onUpdate(dept.code, { availability: "closed" }, `${dept.name}: closed`);

  return (
    <article
      className={cn(
        "card-elevated group relative animate-fade-in overflow-hidden rounded-2xl border border-border p-5 transition",
        "hover:border-border/80"
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div
            className={cn(
              "grid h-11 w-11 place-items-center rounded-xl ring-1",
              "bg-secondary",
              meta.ring
            )}
          >
            <Icon className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-base font-semibold leading-tight">{dept.name}</h3>
            <p className="text-xs text-muted-foreground">
              Code · <span className="font-mono">{dept.code}</span>
            </p>
          </div>
        </div>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium",
            meta.chip
          )}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
          {meta.label}
        </span>
      </div>

      {/* Click-to-complete patient queue. The first row is the patient
          currently being served; subsequent rows are next-up. Clicking any
          row marks that patient's current step done and advances them.
          Optimistic fade gives instant feedback while the API round-trips. */}
      <div className="mt-4">
        <div className="mb-1.5 flex items-baseline justify-between gap-2 px-1">
          <span className="text-[9px] font-semibold uppercase tracking-widest text-muted-foreground">
            Queue
          </span>
          <span className="text-[10px] text-muted-foreground">
            {queue.length === 0
              ? "no patients"
              : queue.length === 1
              ? "1 patient · click to complete"
              : `${queue.length} patients · click any row to complete`}
          </span>
        </div>
        {queue.length === 0 ? (
          <div className="rounded-xl bg-secondary/40 px-3 py-3 text-sm text-muted-foreground ring-1 ring-border/40">
            No patient at this department
          </div>
        ) : (
          <ul className="space-y-1">
            {queue.map((j, idx) => {
              const fading = completingIds.has(j.journey_id);
              const isFirst = idx === 0;
              return (
                <li key={j.journey_id}>
                  <button
                    type="button"
                    title="Click to mark this patient's current test as completed"
                    aria-label={`Complete current step for queue #${j.sequence_number ?? "?"} ${j.display_name ?? ""}`}
                    disabled={fading}
                    onClick={() => onComplete(j.journey_id)}
                    className={cn(
                      "group/row flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left ring-1 transition",
                      fading
                        ? "pointer-events-none scale-[0.98] opacity-30"
                        : "hover:bg-status-active/10 hover:ring-status-active/40 active:scale-[0.99]",
                      isFirst
                        ? "bg-primary/10 ring-primary/25"
                        : "bg-secondary/40 ring-border/40"
                    )}
                  >
                    <span
                      className={cn(
                        "flex shrink-0 flex-col items-center rounded-lg px-2 py-1 leading-none ring-1",
                        isFirst
                          ? "bg-primary/15 text-primary ring-primary/25"
                          : "bg-background/60 text-foreground ring-border/40"
                      )}
                    >
                      <span className="text-[8px] font-semibold uppercase tracking-widest text-muted-foreground">
                        Queue
                      </span>
                      <span className="font-mono text-base font-extrabold tabular-nums">
                        #{j.sequence_number ?? "—"}
                      </span>
                    </span>
                    <div className="min-w-0 flex-1 leading-tight">
                      <div className="truncate text-sm font-semibold">
                        {j.display_name ?? `chat-${j.telegram_chat_id}`}
                      </div>
                      <div className="text-[10px] text-muted-foreground">
                        <span className="uppercase tracking-wider">ID</span>{" "}
                        <span className="font-mono font-semibold text-foreground/80">
                          {j.patient_identifier ?? "—"}
                        </span>
                        {j.current_token && (
                          <>
                            {" · "}
                            <span className="font-mono">{j.current_token}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <span
                      className={cn(
                        "shrink-0 text-[10px] font-medium uppercase tracking-wider transition",
                        fading
                          ? "text-status-active"
                          : "text-muted-foreground/60 group-hover/row:text-status-active"
                      )}
                    >
                      {fading ? "✓ Completed" : isFirst ? "Now Serving" : "Mark done"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="mt-5 grid grid-cols-3 gap-3">
        <Metric label="In queue" value={dept.queue.toString()} />
        <Metric label="Wait" value={`${dept.waitMin}m`} />
        <Metric label="Capacity" value={`${dept.queue}/${dept.capacity}`} />
      </div>

      <div className="mt-4">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-secondary">
          <div
            className={cn(
              "h-full rounded-full transition-all",
              fillPct >= 85
                ? "bg-status-issue"
                : fillPct >= 60
                ? "bg-status-busy"
                : "bg-status-active"
            )}
            style={{ width: `${fillPct}%` }}
          />
        </div>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={addPatient} className="gap-1.5">
          <Plus className="h-4 w-4" /> Add
        </Button>
        <Button size="sm" variant="secondary" onClick={removePatient} className="gap-1.5">
          <Minus className="h-4 w-4" /> Serve
        </Button>

        <div className="ml-auto flex items-center gap-1.5">
          <Input
            type="number"
            value={waitDraft}
            onChange={(e) => setWaitDraft(e.target.value)}
            onBlur={commitWait}
            onKeyDown={(e) =>
              e.key === "Enter" && (e.target as HTMLInputElement).blur()
            }
            className="h-8 w-16 text-center text-sm"
            aria-label="Wait time minutes"
          />
          <span className="text-xs text-muted-foreground">min</span>
        </div>

        {isOpen ? (
          <Button
            size="sm"
            variant="outline"
            onClick={setMaintenance}
            className="gap-1.5 border-status-issue/40 text-status-issue hover:bg-status-issue/10 hover:text-status-issue"
          >
            <Wrench className="h-4 w-4" /> Maintenance
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            onClick={resume}
            className="gap-1.5 border-status-active/40 text-status-active hover:bg-status-active/10 hover:text-status-active"
          >
            <Play className="h-4 w-4" /> Resume
          </Button>
        )}

        {dept.status !== "closed" ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={close}
            className="gap-1.5 text-muted-foreground hover:text-foreground"
          >
            <Pause className="h-4 w-4" /> Close
          </Button>
        ) : null}
      </div>
    </article>
  );
};

const Metric = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded-xl bg-secondary/60 px-3 py-2.5">
    <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
      {label}
    </p>
    <p className="mt-0.5 text-xl font-semibold tracking-tight tabular-nums">{value}</p>
  </div>
);

export default Index;
