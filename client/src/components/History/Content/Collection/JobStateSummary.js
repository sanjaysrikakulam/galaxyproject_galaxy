/**
 * Read-only class takes raw data and applies some rules
 * to determine an amalgam state for a collection.
 *
 * Logic stolen from job-state-model.js and hdca-li.js
 */
import { STATES } from "../model/states";

const NON_TERMINAL_STATES = ["new", "waiting", "queued", "running", "resubmitted", "upload"];
const ERROR_STATES = ["error", "discarded", "deleted"];

export class JobStateSummary extends Map {
    constructor(dsc = {}) {
        const { job_state_summary = {}, populated_state = null } = dsc;
        super(Object.entries(job_state_summary));
        this.populated_state = populated_state;
    }

    hasJobs(key) {
        return this.has(key) ? this.get(key) > 0 : false;
    }

    // any of the passed states has jobs > 0
    anyHasJobs(...states) {
        return states.some((s) => this.hasJobs(s));
    }

    get state() {
        if (this.jobCount) {
            if (this.isErrored) {
                return STATES.error;
            }
            if (this.isRunning) {
                return STATES.running;
            }
            if (this.isNew) {
                return STATES.loading;
            }
            if (this.isTerminal) {
                return STATES.ok;
            }
            return STATES.queued;
        }
        return this.populated_state;
    }

    // Flags

    get isNew() {
        return !this.populated_state || this.populated_state == STATES.new;
    }

    get isErrored() {
        return this.populated_state == STATES.error || this.anyHasJobs(...ERROR_STATES);
    }

    get isTerminal() {
        if (this.isNew) {
            return false;
        }
        return !this.anyHasJobs(...NON_TERMINAL_STATES);
    }

    get isRunning() {
        return this.hasJobs(STATES.running);
    }

    // Counts

    get okCount() {
        return this.get("ok");
    }

    get jobCount() {
        return this.get("all_jobs");
    }

    get errorCount() {
        return (
            (this.get("error") || 0) +
            (this.get("failed") || 0) +
            (this.get("discarded") || 0) +
            (this.get("deleted") || 0)
        );
    }

    get runningCount() {
        return this.get("running");
    }

    get waitingCount() {
        return (this.get("queued") || 0) + (this.get("waiting") || 0) + (this.get("paused") || 0);
    }
}
