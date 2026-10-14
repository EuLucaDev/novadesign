//! Colaboração em tempo real (Nova Live) no motor do Nova Design.
//!
//! - Comandos que entram no desfazer viram [`LiveChange::Op`] com a seleção de quem editou, para o
//!   colega rodar o mesmo comando nos mesmos objetos.
//! - Qualquer outra mudança do documento (desfazer, refazer, arrastar, gestos) vira
//!   [`LiveChange::Replace`]: a revisão do documento mudou sem um comando replicável.
//! - A seleção é pessoal e não entra na impressão digital.

use std::sync::Arc;

use novadesign_doc::{Document, Selection};
use serde_json::{Value, json};

use crate::{EngineError, Result, Session};

#[derive(Clone, Debug, PartialEq)]
pub enum LiveChange {
    Op { cmd: String, params: Value, ctx: Value },
    Replace,
}

/// Estado da colaboração dentro da sessão.
#[derive(Default)]
pub struct LiveState {
    /// uid do documento compartilhado.
    pub doc: Option<u64>,
    pub outbox: Vec<LiveChange>,
    pub(crate) applying: bool,
    /// Revisão já informada à sessão (comandos replicados, documento recebido).
    pub(crate) seen: u64,
    /// Comandos rodando agora (comandos que chamam comandos): só o de fora é replicado.
    pub(crate) depth: u32,
}

fn err(e: impl std::fmt::Display) -> EngineError {
    EngineError::Other(format!("colaboração: {e}"))
}

/// Impressão digital estável (FNV-1a 64).
pub fn fnv64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

// Only audited document edits may execute on a peer. Other local edits travel as data.
fn remote_command_allowed(cmd: &str) -> bool {
    matches!(cmd, "layer.new" | "swatch.addUnnamed")
}

impl Session {
    fn live_index(&self) -> Result<usize> {
        let uid = self.live.doc.ok_or_else(|| err("nenhuma sessão"))?;
        self.documents().iter().position(|d| d.uid == uid).ok_or_else(|| err("o documento compartilhado foi fechado"))
    }

    pub fn live_start(&mut self) -> Result<()> {
        let d = self.doc()?;
        let (uid, rev) = (d.uid, d.revision);
        self.live = LiveState { doc: Some(uid), seen: rev, ..LiveState::default() };
        Ok(())
    }

    pub fn live_stop(&mut self) {
        self.live = LiveState::default();
    }

    pub fn live_active(&self) -> bool {
        self.live.doc.is_some()
    }

    /// Alterações locais desde a última chamada (inclui o que mudou fora de comandos).
    pub fn live_take(&mut self) -> Vec<LiveChange> {
        if let Ok(i) = self.live_index()
            && let Some(d) = self.documents().get(i)
            && d.revision != self.live.seen
        {
            let rev = d.revision;
            self.live.seen = rev;
            self.live.outbox.push(LiveChange::Replace);
        }
        std::mem::take(&mut self.live.outbox)
    }

    /// No meio de um gesto (arrastar, girar, desenhar): o documento inteiro espera.
    pub fn live_busy(&self) -> bool {
        self.live_index().ok().and_then(|i| self.documents().get(i)).is_some_and(|d| d.interaction.is_some())
    }

    pub fn live_snapshot(&self) -> Result<Vec<u8>> {
        let d = self.documents().get(self.live_index()?).ok_or_else(|| err("sem documento"))?;
        serde_json::to_vec(&*d.doc).map_err(err)
    }

    pub fn live_digest(&self) -> Result<u64> {
        Ok(fnv64(&self.live_snapshot()?))
    }

    pub fn live_load(&mut self, bytes: &[u8]) -> Result<()> {
        let i = self.live_index()?;
        let doc: Document = serde_json::from_slice(bytes).map_err(err)?;
        let prev = self.active_index();
        self.set_active(i);
        let st = self.doc_mut()?;
        st.doc = Arc::new(doc);
        st.revision += 1;
        keep_valid(st);
        let rev = st.revision;
        self.live.seen = rev;
        if let Some(p) = prev {
            self.set_active(p);
        }
        Ok(())
    }

    /// Roda o comando de um colega com a seleção dele; o desfazer e a seleção locais ficam como estavam.
    pub fn live_apply(&mut self, cmd: &str, params: &Value, ctx: &Value) -> Result<()> {
        if !remote_command_allowed(cmd) { return Err(err("comando não permitido na colaboração")); }
        let i = self.live_index()?;
        let prev = self.active_index();
        self.set_active(i);
        let (local_sel, undo_len) = {
            let d = self.doc()?;
            (d.selection.clone(), d.history.undo.len())
        };
        if let Some(sel) = ctx.get("selection").and_then(|s| serde_json::from_value::<Selection>(s.clone()).ok()) {
            self.doc_mut()?.selection = sel;
        }
        self.live.applying = true;
        let r = self.execute(cmd, params);
        self.live.applying = false;
        if let Ok(st) = self.doc_mut() {
            st.history.undo.truncate(undo_len);
            st.selection = local_sel;
            keep_valid(st);
            let rev = st.revision;
            self.live.seen = rev;
        }
        if let Some(p) = prev {
            self.set_active(p);
        }
        r.map(|_| ())
    }

    /// Chamado por `execute` depois de cada comando externo (não aninhado).
    pub(crate) fn live_record(&mut self, id: &str, params: &Value, replicable: bool, ctx: Value) {
        if self.live.applying || self.live.depth > 1 {
            return;
        }
        let Ok(i) = self.live_index() else { return };
        let Some(d) = self.documents().get(i) else { return };
        let rev = d.revision;
        if rev == self.live.seen {
            return;
        }
        if replicable && remote_command_allowed(id) {
            self.live.outbox.push(LiveChange::Op { cmd: id.to_string(), params: params.clone(), ctx });
            self.live.seen = rev;
        }
        // Não replicável: `live_take` manda o documento inteiro.
    }
}

/// Seleção e camada ativa continuam válidas depois de um documento ou comando de outra pessoa.
fn keep_valid(st: &mut crate::DocState) {
    let doc = st.doc.clone();
    st.selection.items.retain(|i| doc.item(*i).is_some());
    if doc.layer(st.active_layer).is_none() {
        st.active_layer = doc.default_layer();
    }
}

pub(crate) fn context(selection: &Selection) -> Value {
    json!({ "selection": selection })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    #[test]
    fn remote_commands_cannot_open_export_or_execute_code() {
        let (_, mut b) = pair();
        let before = b.live_digest().unwrap();
        for command in ["file.open", "file.saveAs", "file.export", "session.open", "script.run", "app.quit", "lisp.eval"] {
            assert!(b.live_apply(command, &serde_json::json!({"path": "/tmp/not-authorized"}), &serde_json::json!({})).is_err());
            assert_eq!(before, b.live_digest().unwrap());
        }
    }

    fn pair() -> (Session, Session) {
        let mut a = Session::new();
        a.execute("file.new", &json!({})).unwrap();
        a.live_start().unwrap();
        let mut b = Session::new();
        b.execute("file.new", &json!({})).unwrap();
        b.live_start().unwrap();
        let snap = a.live_snapshot().unwrap();
        b.live_load(&snap).unwrap();
        (a, b)
    }

    fn sync(a: &mut Session, b: &mut Session) {
        for change in a.live_take() {
            match change {
                LiveChange::Op { cmd, params, ctx } => b.live_apply(&cmd, &params, &ctx).unwrap(),
                LiveChange::Replace => {
                    let snap = a.live_snapshot().unwrap();
                    b.live_load(&snap).unwrap();
                }
            }
        }
        assert!(b.live_take().is_empty(), "o que veio de A não volta para A");
    }

    #[test]
    fn edits_replicate_and_undo_travels_as_a_document() {
        let (mut a, mut b) = pair();
        a.execute("layer.new", &json!({})).unwrap();
        a.execute("swatch.addUnnamed", &json!({})).unwrap();
        sync(&mut a, &mut b);
        assert_eq!(a.live_digest().unwrap(), b.live_digest().unwrap());
        assert!(b.doc().unwrap().history.undo.is_empty());

        a.execute("edit.undo", &json!({})).unwrap();
        sync(&mut a, &mut b);
        assert_eq!(a.live_digest().unwrap(), b.live_digest().unwrap());

        b.execute("layer.new", &json!({})).unwrap();
        sync(&mut b, &mut a);
        assert_eq!(a.live_digest().unwrap(), b.live_digest().unwrap());
    }
}
