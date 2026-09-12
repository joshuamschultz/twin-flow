# Twinflow capability specifications from Western Spring

Status: accepted requirements and historical assessment baseline for the `0.4.0a1`
production capability alpha. The ten typed core contracts are implemented and traced in
[`VALIDATION.md`](VALIDATION.md). Source assessment baseline:
`ab7d34b01093e78e1dab92a8943041745eeda9be`, package `0.3.0a1`. Paths and source findings
below refer to that baseline. The standalone project continues to consume a pinned pip
installation; these specifications do not authorize application-owned replacements for
core scheduling or simulation.

The **Evidence** paragraphs below are historical snapshot evidence from the assessment,
not claims that the current alpha has been calibrated or accepted on the plant floor.
The **Historical Western Spring standalone alpha treatment** paragraphs describe the
application's pre-core approximation at assessment time. They are retained as provenance,
not as the current library implementation status. Use the release validation matrix for
current source and test evidence.

The hard-constraint scheduler already handles shared named resources, operation precedence, eligibility, qualifications, readiness times, frozen assignments and resource windows. These are **not gaps**. The alpha uses them to schedule seven orders jointly. The DES engine separately has stock, setup, batch-hold, quality, labor and shift primitives. Each specification below states the missing composition or semantics, rather than pretending those primitives do not exist.

Priorities: P0 before a faithful joint production/WIP model; P1 before delivery planning with real constraints; P2 after initial calibration. A business-required behavior can be partially approximated in the alpha while its core specification remains open. All acceptance examples use minutes unless otherwise stated.

## TF-WS-001 — Coupled production resources and conveyor pipeline

**Priority:** P0. **Evidence:** transcript 20:18–20:55 (attached decoiler), 22:10–22:35 (robot assist), 26:10–27:20 (paired conveyor, throughput inherited, 3–8-minute dwell). `scheduling/core.py:14` Operation has one eligible resource set and `ScheduleResult.resources` one resource per operation. There is no multi-resource occupancy, predecessor transit lag or pipeline contract. `plan/driver.py:668` wires whole bundles downstream; it does not stream the first/last piece through a coupled oven.

**Requirement:** A process SHALL declare a primary resource plus simultaneous auxiliary requirements with their own occupied phases. A conveyor SHALL declare transit dwell separately from throughput and couple to the upstream production interval without charging a second per-piece processing duration. A paired decoiler SHALL not add independent serial runtime or extra coiling capacity. A reused movable auxiliary SHALL contend globally.

**Proposed model:** `resource_requirements[{role, eligible_ids, quantity, phase_start, phase_end}]`; explicit process phases; `pipeline{upstream_operation, dwell, transfer_unit, capacity_rule, pairing}`. Machine assignment and auxiliary pairing remain separate constraints. Calendar and occupation rules specify whether the oven remains occupied during the production tail. Zero-duration milestones and lag edges should be first-class if needed, rather than fake epsilon jobs. Preserve per-phase resource usage in the verified output.

**Acceptance:** Two jobs sharing one oven cannot occupy it concurrently even when their coilers differ; two independently paired coilers may run concurrently. Given an upstream last-piece exit at t = 60 and dwell 5, last downstream exit is t = 65; doubling quantity affects upstream runtime but does not multiply dwell. A second job cannot claim a paired oven while it is still carrying the first job's pipeline tail. Adding an attached decoiler changes no route duration without explicit handling work. One robot cannot attend two required phases simultaneously. The verifier detects a forged auxiliary overlap.

**Historical Western Spring standalone alpha treatment:** Dedicated fixed 3/5/8-minute tail-delay nodes; actual paired-oven capacity and robot simultaneity not enforced. Timing is provisional. **Implementation order:** First implement TF-WS-001's shared phase/multi-resource contract; attendance and pipeline behavior build on it. TF-WS-002 is a coordinated consumer of that foundation, not a prerequisite. Streaming partial-lot transfer additionally requires TF-WS-004.

## TF-WS-002 — Phase-specific labor, shift pauses and restart

**Priority:** P0. **Evidence:** transcript 08:25–09:00 and 13:37–14:04. `scheduling/core.py:14–36` chooses one resource and requires one continuous window per operation (`_place`). It cannot jointly reserve a machine and skilled person or pause/restart an interval. DES `primitives/location.py:399–410` already releases labor during `finish_unattended` runtime and reacquires for unload; `LocationSpec.shift_crossing` supports pause/unattended/overtime, but neither scheduler nor this public phase contract describes a machine-specific warm restart after idle.

**Requirement:** Machine occupation SHALL be independent of attended labor. Setup, material pull, load, checks, restart and unload SHALL request applicable skills and staffing; unattended processing SHALL hold the machine while releasing labor. Crossing a shift SHALL obey explicit pause, permitted unattended completion or approved overtime policy. Idle/cold restart SHALL incur a configurable adjustment phase, with provenance.

**Proposed model:** `attendance_phases[{kind, duration_model, skills, headcount}]`, `machine_hold`, `shift_crossing`, and `restart_rule{idle_threshold, duration, labor_requirement, applicability}`. Scheduler resource requirements link to actual labor pools or named qualifications. Avoid treating a machine's “qualification” field as proof an operator is available.

**Acceptance:** One qualified setter can set up two machines sequentially while their unattended runs overlap; setup intervals never overlap. A 120-minute job with 60 working minutes left and pause policy resumes remaining 60 next shift, without repeating the completed work. A configured 10-minute cold restart is charged once before resumed production. An unattended run may finish off shift but required unload waits for the next shift. Machine and labor usage totals reconcile separately. A skill mismatch rejects a candidate even if a machine is idle.

**Historical Western Spring standalone alpha treatment:** 24/7 machine availability, unconstrained labor, full setup on remaining operations. These are estimates, not an inferred staffing plan. **Dependencies:** TF-WS-001's multi-resource phase foundation, not its completed conveyor feature; collect actual calendars/skills first.

## TF-WS-003 — Oven batch capacity, recipe compatibility and warm-up

**Priority:** P0. **Evidence:** transcript 26:44–28:14, 29:47–29:51. `primitives/time_model.py` supports `batch_hold` independent of quantity; `primitives/location.py:375–379` uses the first selected bundle for setup/time and explicitly defers mixed-thing batching. `scheduling/core.py.Operation` has no capacity, recipe or batch-membership fields. A fixed whole-WO duration alone cannot verify load capacity or compatible jobs.

**Requirement:** Thermal operations SHALL declare recipe, maximum load capacity with explicit units, minimum hold and warm-up rules. Compatible lots MAY share a batch, retaining order attribution. Incompatible temperature/atmosphere/fixture/material requirements SHALL not share a load. Repeated heat treatments SHALL be distinct visits to shared physical ovens. Low quantity SHALL not imply physically impossible short heating.

**Proposed model:** `batch_resource{capacity,value_uom,compatible_recipe}`, `thermal_recipe{temperature,temperature_uom,warmup,hold,cooldown,loading,unloading}`, `batch{member_lots,quantity_by_uom}` and explicit start policy. Capacity might be weight, basket count or effective volume; do not force piece count. Warmup depends on initial oven state only when measured/configured.

**Acceptance:** Two compatible lots each occupying 40 units can share a 100-unit-capacity batch with one 60-minute hold. A 70+40 load exceeds 100 and splits. Different recipes cannot co-load. A one-piece load still receives the full minimum hold. Two WC310 route visits share the same oven and cannot overlap. Completed quantity is attributed to original member orders, with no merging away identity. Verifier checks capacity and recipe compatibility independently.

**Historical Western Spring standalone alpha treatment:** Fixed warm-up+hold per entire remaining operation; quantity capacity and cross-order batching unknown. **Dependencies:** TF-WS-004 identity/quantity accounting; TF-WS-002 thermal/attendance calendars.

## TF-WS-004 — Remaining WIP, split lots and exact order attribution

**Priority:** P0. **Evidence:** dispatch WO51929 row 42 complete operation, WO52077 rows 37/38/210/270 and WO51735 varying downstream remaining quantities. `plan/loader.py.WorkOrder` includes initial WIP quantity/location/remaining_time, but `plan/driver.py:686–695` `_seed_wip` enqueues a bundle without order attrs and does not use remaining_time. Standard demand release is still separately registered. The initial-WIP surface therefore does not establish resumable, attributed state. Scheduler operations have precedence/duration but no input/output quantity conservation.

**Requirement:** A snapshot SHALL distinguish finished good quantity, queued WIP, active WIP, scrap and unreleased demand by order and operation. Active operations SHALL resume from remaining processing time and actual machine/setup state. Existing WIP SHALL not trigger duplicate raw-material release. Partial transfer SHALL allow ready downstream lots to proceed without waiting for all upstream remaining work.

**Proposed model:** `wip_lots{lot_id,order_id,operation_id,qty,uom,state,location,machine_id,remaining_time,setup_state,as_of,source}` plus `order_demand{target,completed_good,unreleased}`. Reconcile source snapshots through an adapter that reports ambiguous control-point fields; no implicit subtraction rule may overwrite supplied ERP remaining quantities. Track material/production lots separately when units differ.

**Acceptance:** An order requiring 100 with 60 accepted and 40 WIP schedules only 40 remaining; it does not release another 100 raw units. An active job with 10 minutes remaining resumes for 10, preserving order ID and machine. A completed earlier operation is not rerun. Separate 20- and 30-piece WIP lots at different stages can flow independently and yield 50 accepted total without double count. A contradictory snapshot is reported as unresolved, not silently made consistent. Event and final order ledgers reconcile accepted/scrap/in-system quantities to inputs.

**Historical Western Spring standalone alpha treatment:** Preserve each operation's source remaining quantity, omit remaining-zero stages, then schedule remaining stages sequentially with a full setup assumption. This deliberately does not reconstruct overlap among physical WIP lots. **Dependencies:** engine identity/ledger work precedes stochastic WIP validation.

## TF-WS-005 — Physical resources shared across DES recipes

**Priority:** P0 for DES expansion; joint scheduler already supports shared physical IDs. **Evidence:** `model/schema.py:71–78` LocationSpec binds machine/time/setup/transform to a location; `model/compile.py._compile_location` fixes a single setup key/time model per location; `plan/driver.py:568–590` copies a machine and creates a new `simpy.PriorityResource` for every location. Repeating the same machine name across operation-specific locations does not create shared physical capacity.

**Requirement:** The DES model SHALL separate resource identity from route operation/recipe definitions. Multiple recipes and repeated visits SHALL acquire the same physical machine pool and setup state when referencing that machine. Distinct machines at one work center SHALL retain separate state and eligibility. The same modeled resource SHALL mean the same capacity in scheduling and simulation.

**Proposed model:** Global resource registry bound once per replication; route operations reference resource selectors and recipe/time/setup definitions. Add part/operation-specific transformation and dispatch context without cloning resource capacity. Preserve current one-location models as a compatibility subset. Use a canonical resource/operation intermediate contract shared by scheduler and DES adapters.

**Acceptance:** Two 60-minute operation recipes assigned to machine 441 cannot overlap in DES; available makespan is at least 120 absent other effects. Two separate machines can overlap. A return visit observes physical setup state left by other work. Reusing a machine reference does not increment capacity or reset state. The same named-resource constraints verify for both adapters. Independent replications retain no mutable state from prior runs.

**Historical Western Spring standalone alpha treatment:** Use existing joint scheduling.core directly; do not compile operation-specific DES locations and claim accurate shared capacity. **Dependencies:** coordinate canonical phase/recipe design with TF-WS-001/002/006.

## TF-WS-006 — Machine-specific recipes, full setup and preferences

**Priority:** P1; full-setup arithmetic already works in the scheduler alpha. **Evidence:** transcript 10:36–12:00, 37:18–41:25; email explicitly requests full setup for all changes. `scheduling/core.py.Operation.duration` is independent of resource and has no preference/transition cost. `primitives/cell.py.SetupPolicy.changeover` returns zero for the same setup group; YAML compiler sets one setup_key for all consumed inputs at a location. Hard `eligible_resources` exists and is not a gap.

**Requirement:** A recipe SHALL carry per-machine timing/setup and eligibility evidence. Hard restrictions SHALL be independent of soft machine preference. Full setup SHALL be chargeable at every job change even when material/tool/family match; an exception requires an explicit approved rule. Setup-sheet revisions SHALL determine the applicable recipe, not globally mutate historical runs.

**Proposed model:** `operation_alternatives[{machine_id,setup_duration,run_duration,preference_cost,recipe_revision}]`, `setup_policy{mode: full_per_job|transition|retain, source}`, and evidence-backed eligibility rules. Tie preference to a documented optimization objective/tiebreaker; enumerate infeasible alternatives clearly. Operator edits create proposed knowledge revisions until reviewed by the application.

**Acceptance:** A 100-piece job with 60-minute runtime on 343 and 40 on 441 is scheduled and verified using the chosen machine's timing. If only 529 is eligible, no other WC10 machine is selected. When 441 and 343 are equally feasible and preference has a positive configured priority, 441 wins; if 441 is unavailable, a feasible 343 remains allowed. Two same-wire jobs in full_per_job mode both pay setup. A changed recipe revision affects new runs while old results retain their source revision.

**Historical Western Spring standalone alpha treatment:** Explicit allowed-machine overrides supported; one estimated duration across alternatives; general preference for 441 is recorded but unapplied, with no invented 529-only part list. **Dependencies:** TF-WS-005 DES resource separation; calibration of part-machine setup sheets.

## TF-WS-007 — Coil identity, mass accounting and multi-material readiness

**Priority:** P1. **Evidence:** transcript 09:27–09:43 and 14:29–18:05, user email multimat rows. Existing `primitives/stock.py:12–46` maintains a level for one material/UOM; Bundle supports quantity/attributes, and transforms support ratios. `model/schema.py:98` exposes one secondary MaterialSpec per location, while scheduler provides only scalar material_ready_time. These do not constitute a complete coil-selection/remnant/reweigh and multi-material reservation workflow.

**Requirement:** Material reservations SHALL reference identifiable coils/lots with measured mass and material specification. An operation SHALL require all its materials without duplicate processing or partial deadlocking reservations. Setup/damaged-wire mass scrap, run mass, good pieces, downstream lost pieces and returned remnant mass SHALL be separately accounted. Overproduction SHALL be an explicit policy, not a fixed implicit scrap factor.

**Proposed model:** `material_lot{lot_id,item_id,spec,available_qty,uom,quality_state,availability_time}`, `requirements[{item_id,qty,uom,basis}]`, `conversion{part_id,revision,mass_per_piece,source}`, `consumption_event`, `scrap_event` and `reweigh_event{measured_mass,measurement_time}`. Reserve required sets atomically or use a deadlock-safe documented protocol. Coil run-out decisions use configurable marginal-remnant criteria; reject unverified conversions.

**Acceptance:** Loading 1000 lb and accounting 455 lb production+40 lb setup scrap returns 505 lb unless a measured reweigh adjustment is explicitly recorded. No balance becomes negative. Two jobs cannot both reserve the same coil mass. A two-material operation waits until both are available and runs once. Updating a coil's measured remaining weight preserves its ID and prior ledger. Unsupported lb→piece conversion fails with a missing-data error. A near-empty coil policy may overproduce only with explicit extra quantity and inventory outcome.

**Historical Western Spring standalone alpha treatment:** Store material IDs/provenance; qty/UOM remain unknown, availability assumed. No fabricated stock ledger. **Dependencies:** TF-WS-004 lot identity and measured material data.

## TF-WS-008 — Setup sample qualification and rework feedback

**Priority:** P1. **Evidence:** transcript 12:05–13:34. DES `model/schema.py.QualityGateSpec` supports probabilistic output routing; it does not describe a setup qualification state that releases the main production lot only after trial samples traverse later operations and testing. No such state exists on scheduler Operation.

**Requirement:** Setup samples SHALL be traceable to part-machine-recipe revision and may traverse required downstream transformations before test. Main production SHALL wait for qualification when required. Failed samples SHALL return an adjustment task to the originating setup while preserving consumed time/material; an approved known-green shortcut SHALL be explicit and versioned.

**Proposed model:** `qualification_plan{sample_qty,test_route,acceptance_spec,shortcut_evidence,max_iterations}`, setup state (`unqualified`, `sampling`, `awaiting_test`, `adjusting`, `approved`, `failed`), sample lot IDs and test/adjustment events. Measurements and limits are data; the simulation may sample outcomes only under an explicitly stated estimated probability.

**Acceptance:** Main production cannot begin while a required sample is awaiting downstream test. One failed qualification consumes its setup/test resources and creates one adjustment iteration without duplicating the main order. A configured iteration limit produces an unresolved outcome instead of an infinite loop. Approved green-dimension evidence skips only the declared sample operations and is recorded in the run. No automatic note promotion changes qualification requirements silently.

**Historical Western Spring standalone alpha treatment:** A flat full-setup estimate absorbs unknown qualification effort; no assertion that trial loops were simulated. **Dependencies:** TF-WS-001/004 and actual production-aid documents.

## TF-WS-009 — Outside processing, partial returns and shipment identity

**Priority:** P1. **Evidence:** transcript 17:18–18:52, 30:19–30:31; WO51997 includes shipping 0030, outside 0040, shipping 0070. Scheduler's single duration/resource can represent an estimated vendor hold, but lacks lot shipment/return/yield states. DES quality gates can route good/scrap, yet `plan/driver.py._WorkTracker.outcomes` sets `shipped_qty` equal to accepted quantity, which is not proof of a customer shipment.

**Requirement:** Outside processing SHALL distinguish outbound supplier shipment, vendor processing, receipt/inspection, accepted/rejected/lost quantities, and later customer shipment. Vendor turnaround and capacity MAY be declared independently. Partial returns SHALL feed downstream only with available accepted quantity. Production completion SHALL not automatically equal customer shipment.

**Proposed model:** `external_operation{vendor,service,dispatch_calendar,transit_out,queue/process,transit_in,yield_model}`, external lot events and explicit terminal semantics (`produced`, `quality_accepted`, `shipped_to_vendor`, `shipped_to_customer`). Keep nondestructive-inspection rejection distinct from plating handling loss. Inspection method from transcript is ambiguous and must be confirmed rather than hardcoded as a technical standard.

**Acceptance:** Shipping an outbound lot does not complete the customer order. If 100 pieces leave and 95 return accepted with 5 lost, downstream receives 95 and the shortage remains 5. Two vendors can have independent calendars/capacity; an unknown-capacity delay mode does not fabricate congestion. Partial accepted receipts may start downstream while the remaining lot is pending. Customer-shipped quantity changes only on the explicit customer-shipping event.

**Historical Western Spring standalone alpha treatment:** Order-specific fixed turnaround resource, no vendor capacity or yield simulation; completion is end of selected remaining route, not promised/customer-shipped quantity. **Dependencies:** TF-WS-004 quantity ledger, TF-WS-007 material/lot identity.

## TF-WS-010 — Per-order commitments and backlog scope

**Priority:** P2 after data enrichment. **Evidence:** transcript 06:24–08:15 and supplied email: WOs serve sales orders or replenishment, MRP demand without WOs is missing. `scheduling/core.py.SchedulingProblem` has a global deadline; `_objective` lateness is makespan minus that global deadline, not individual-order lateness. DES WorkOrder due_date exists, so due dates in the library are not generally absent.

**Requirement:** Scheduling SHALL represent order/customer commitment linkage, distinct release/demand/production/ship dates, and objective terms for individual order tardiness/service priority. A scenario SHALL state the included backlog and snapshot time, preserving unfinished demands and replenishment distinctions. Missing commitment dates SHALL not become invented on-time metrics.

**Proposed model:** `jobs{job_id,operation_ids,completion_operations,demand_kind,promised_ship_time,priority,sales_order_refs}`, optional order-level objectives and comparison cohort metadata. The application already owns input provenance and selection; add core job/objective semantics without duplicating that application responsibility.

**Acceptance:** Two orders with different due dates produce independently correct tardiness, including on-time completion of one while the other is late. Inventory replenishment without a promise is excluded from customer on-time percentage and remains visible in production load. A seven-of-126 scenario is explicitly distinguishable from all 126 plus pending MRP demand. Comparing cases with different cohorts fails or labels the mismatch. Historical ERP operation End Date never silently populates promised_ship_time.

**Historical Western Spring standalone alpha treatment:** Makespan and per-WO elapsed completion only; selected seven versus omitted 119 visible. No claimed on-time customer metric.

## Implementation boundary and review gates

The standalone project owns input parsing, normalization, estimates, documented
overrides, report generation, and assumption/source versioning. Core owns the typed
scheduling, resource, quantity, time, and independent verification semantics. No
application-specific simulator, labor-as-machine semaphore, duplicated physical-machine
pool, or postprocessed schedule substitutes for these core contracts.

The release includes positive and forged-invalid tests for the declared constraints and
preserves compatibility for existing model/plan behavior. A passing solver result remains
distinct from empirical validation against Western Spring. Review these specifications
with plant staff as calibrated calendars, recipes, capacity data, and commitments become
available; unresolved business data remains visible rather than being filled by engine
policy.
