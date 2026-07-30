import { ConcreteRunner, TS_ANSWER } from "./typescript_symbols";

export function execute(): string {
  return `${new ConcreteRunner().run()}:${TS_ANSWER}`;
}
