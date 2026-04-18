// A small monolith-ish module: multiple responsibilities, event-driven dispatch.
const STATUS_DONE = "DONE";
const STATUS_IN_PROGRESS = "IN_PROGRESS";

function reduce(state, action) {
  switch (action.type) {
    case "START":
      return { ...state, status: STATUS_IN_PROGRESS, evidence: [] };
    case "FINISH":
      return { ...state, status: STATUS_DONE, evidence: action.evidence };
    default:
      return state;
  }
}

// Env use
const API_KEY = process.env.API_KEY;

module.exports = { reduce, STATUS_DONE, STATUS_IN_PROGRESS };
