import { helper } from "./util";

export interface UserRecord {
  status: string;
  evidence: string[];
}

export type Handler = (u: UserRecord) => void;

export class App {
  start(): void {
    const apiKey = process.env.API_KEY;
    helper(apiKey || "");
  }
}

export const VERSION = "1.0";
