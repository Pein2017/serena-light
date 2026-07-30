export interface Runner {
  run(): string;
}

export class ConcreteRunner implements Runner {
  run(): string {
    return "ready";
  }
}

export const TS_ANSWER: number = 42;
