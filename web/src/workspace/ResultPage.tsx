// A workspace results page on the standard layout (DESIGN.md 0.1): top bar, section bar,
// answer hero, then question sections. It only adds the workspace shell to `Page`.
import type { ReactNode } from "react";
import { Page, type SectionRef } from "../decision/Page";
import { ShellContext } from "../decision/viewState";

export function ResultPage(props: {
  view: string;
  title: string;
  project: string;
  scope: string;
  sections: SectionRef[];
  hero: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { hero, children, ...page } = props;
  return (
    <ShellContext.Provider value="workspace">
      <Page {...page}>
        {hero}
        <div className="tf-wrap">{children}</div>
      </Page>
    </ShellContext.Provider>
  );
}
