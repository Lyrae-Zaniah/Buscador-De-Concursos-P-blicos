#!/usr/bin/env python3
"""
Interface gráfica do Buscador de Concursos.

Coloque este arquivo na MESMA PASTA do buscador_concursos.py e execute:
    python buscador_gui.py

Dependências: python -m pip install requests beautifulsoup4   (Tkinter já vem com o Python)
"""
from __future__ import annotations

import contextlib
import queue
import re
import sys
import threading
import webbrowser
from datetime import date
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import buscador_concursos as bc
except ImportError as e:  # pragma: no cover
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Arquivo não encontrado",
        "Não consegui importar 'buscador_concursos.py'.\n"
        "Deixe os dois arquivos na mesma pasta e instale as dependências:\n"
        "python -m pip install requests beautifulsoup4\n\n" + str(e),
    )
    sys.exit(1)

TIPOS_SUGERIDOS = ["policia", "saude", "educacao", "tribunal", "ti", "militar", "banco"]

COLUNAS = [  # (id, título, largura, alinhamento)
    ("prazo", "Inscrições até", 105, "center"),
    ("dias", "Dias", 55, "center"),
    ("uf", "UF", 40, "center"),
    ("orgao", "Órgão", 250, "w"),
    ("cargos", "Cargos / descrição", 340, "w"),
    ("vagas", "Vagas", 60, "center"),
    ("salario", "Salário", 110, "e"),
    ("nivel", "Nível", 110, "w"),
    ("fonte", "Fonte", 60, "center"),
]


class _FilaLog:
    """Recebe os print() da busca (rodando em outra thread) e manda para a interface."""

    def __init__(self, fila):
        self.fila = fila

    def write(self, s):
        for linha in str(s).splitlines():
            if linha.strip():
                self.fila.put(("log", linha.strip()))
        return len(s)

    def flush(self):
        pass


def _corrigir_cores_treeview(style):
    """Workaround para o bug de cores de tag do Treeview em alguns Tk 8.6.9."""
    def filtrar(opcao):
        return [e for e in style.map("Treeview", query_opt=opcao) if e[:2] != ("!disabled", "!selected")]
    style.map("Treeview", foreground=filtrar("foreground"), background=filtrar("background"))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Buscador de Concursos Públicos")
        root.geometry("1250x760")
        root.minsize(980, 600)

        self.fila: queue.Queue = queue.Queue()
        self.resultados: list = []
        self.ordem_col = "prazo"
        self.ordem_inv = False
        self.buscando = False

        style = ttk.Style()
        _corrigir_cores_treeview(style)
        style.configure("Treeview", rowheight=26)
        style.configure("Titulo.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Dica.TLabel", foreground="#666666")

        self._montar_filtros()
        self._montar_tabela()
        self._montar_detalhes()
        self._montar_status()

        root.bind("<Return>", lambda e: self.iniciar_busca())
        self.ent_tipo.focus_set()

    # ------------------------------------------------------------------ UI
    def _montar_filtros(self):
        topo = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        topo.pack(fill="x")
        ttk.Label(topo, text="Buscador de Concursos Públicos", style="Titulo.TLabel").grid(
            row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))

        ttk.Label(topo, text="Tipo de concurso").grid(row=1, column=0, sticky="w")
        ttk.Label(topo, text="Estado").grid(row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Label(topo, text="Cidade (opcional)").grid(row=1, column=2, sticky="w", padx=(10, 0))

        self.var_tipo = tk.StringVar()
        self.ent_tipo = ttk.Combobox(topo, textvariable=self.var_tipo, values=TIPOS_SUGERIDOS, width=26)
        self.ent_tipo.grid(row=2, column=0, sticky="we")

        self.var_uf = tk.StringVar(value="Brasil inteiro")
        valores_uf = ["Brasil inteiro"] + [f"{s} - {n}" for s, n in bc.UFS.items()]
        self.cb_uf = ttk.Combobox(topo, textvariable=self.var_uf, values=valores_uf, width=26, state="readonly")
        self.cb_uf.grid(row=2, column=1, sticky="we", padx=(10, 0))
        self.cb_uf.bind("<<ComboboxSelected>>", self._ao_mudar_estado)

        self.var_cidade = tk.StringVar()
        self.ent_cidade = ttk.Entry(topo, textvariable=self.var_cidade, width=24, state="disabled")
        self.ent_cidade.grid(row=2, column=2, sticky="we", padx=(10, 0))

        opc = ttk.Frame(topo)
        opc.grid(row=2, column=3, padx=(14, 0), sticky="w")
        self.var_fontes = {"pci": tk.BooleanVar(value=True), "atlas": tk.BooleanVar(value=True)}
        ttk.Checkbutton(opc, text="PCI Concursos", variable=self.var_fontes["pci"]).pack(side="left")
        ttk.Checkbutton(opc, text="Atlas Público", variable=self.var_fontes["atlas"]).pack(side="left", padx=(8, 0))
        self.var_vencidos = tk.BooleanVar(value=False)
        ttk.Checkbutton(opc, text="Incluir encerrados", variable=self.var_vencidos).pack(side="left", padx=(14, 0))

        ttk.Label(opc, text="Páginas:").pack(side="left", padx=(14, 2))
        self.var_paginas = tk.IntVar(value=5)
        ttk.Spinbox(opc, from_=1, to=30, width=4, textvariable=self.var_paginas).pack(side="left")

        self.btn_buscar = ttk.Button(topo, text="🔍  Buscar", command=self.iniciar_busca)
        self.btn_buscar.grid(row=2, column=4, padx=(14, 0), ipadx=10)
        self.btn_csv = ttk.Button(topo, text="Exportar CSV", command=self.exportar_csv, state="disabled")
        self.btn_csv.grid(row=2, column=5, padx=(8, 0))

        ttk.Label(
            topo, style="Dica.TLabel",
            text="Dica: separe vários tipos por vírgula (ex.: policia, bombeiro). "
                 "Termos como 'policia' e 'saude' já incluem sinônimos."
        ).grid(row=3, column=0, columnspan=8, sticky="w", pady=(4, 0))
        topo.columnconfigure(6, weight=1)

    def _montar_tabela(self):
        meio = ttk.Frame(self.root, padding=(12, 6, 12, 0))
        meio.pack(fill="both", expand=True)

        barra = ttk.Frame(meio)
        barra.pack(fill="x", pady=(0, 4))
        ttk.Label(barra, text="Filtrar nos resultados:").pack(side="left")
        self.var_filtro = tk.StringVar()
        self.var_filtro.trace_add("write", lambda *_: self.renderizar())
        ttk.Entry(barra, textvariable=self.var_filtro, width=30).pack(side="left", padx=6)
        self.lbl_contagem = ttk.Label(barra, text="", style="Dica.TLabel")
        self.lbl_contagem.pack(side="right")

        quadro = ttk.Frame(meio)
        quadro.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(quadro, columns=[c[0] for c in COLUNAS], show="headings", selectmode="browse")
        for cid, titulo, larg, anc in COLUNAS:
            self.tree.heading(cid, text=titulo, command=lambda c=cid: self.ordenar_por(c))
            self.tree.column(cid, width=larg, anchor=anc, stretch=cid in ("orgao", "cargos"))
        ys = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(quadro, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        quadro.rowconfigure(0, weight=1)
        quadro.columnconfigure(0, weight=1)

        self.tree.tag_configure("hoje", background="#ffd6d6")
        self.tree.tag_configure("urgente", background="#fff0d6")
        self.tree.tag_configure("encerrado", foreground="#999999")
        self.tree.bind("<<TreeviewSelect>>", self._ao_selecionar)
        self.tree.bind("<Double-1>", lambda e: self.abrir_detalhes())

    def _montar_detalhes(self):
        baixo = ttk.LabelFrame(self.root, text=" Detalhes do concurso selecionado ", padding=8)
        baixo.pack(fill="x", padx=12, pady=(8, 4))

        self.txt = tk.Text(baixo, height=7, wrap="word", relief="flat", state="disabled",
                           font=("Segoe UI", 10), padx=4, pady=2)
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("rotulo", font=("Segoe UI", 10, "bold"))
        self.txt.tag_configure("titulo", font=("Segoe UI", 12, "bold"))
        self.txt.tag_configure("alerta", foreground="#b00020", font=("Segoe UI", 10, "bold"))

        bt = ttk.Frame(baixo)
        bt.pack(side="right", padx=(10, 0), anchor="n")
        self.btn_det = ttk.Button(bt, text="Abrir página do edital", command=self.abrir_detalhes, state="disabled")
        self.btn_ins = ttk.Button(bt, text="Abrir inscrição oficial", command=self.abrir_inscricao, state="disabled")
        self.btn_cp = ttk.Button(bt, text="Copiar link", command=self.copiar_link, state="disabled")
        for b in (self.btn_det, self.btn_ins, self.btn_cp):
            b.pack(fill="x", pady=2)
        self._escrever_detalhe(None)

    def _montar_status(self):
        barra = ttk.Frame(self.root, padding=(12, 2, 12, 8))
        barra.pack(fill="x")
        self.progress = ttk.Progressbar(barra, mode="indeterminate", length=120)
        self.progress.pack(side="left")
        self.lbl_status = ttk.Label(barra, text="Pronto. Informe o tipo de concurso e clique em Buscar.")
        self.lbl_status.pack(side="left", padx=10)
        ttk.Label(barra, style="Dica.TLabel",
                  text="Confira sempre o edital oficial antes de se inscrever.").pack(side="right")

    # ------------------------------------------------------------ Eventos
    def _ao_mudar_estado(self, _evt=None):
        if self.var_uf.get().startswith("Brasil"):
            self.var_cidade.set("")
            self.ent_cidade.config(state="disabled")
        else:
            self.ent_cidade.config(state="normal")
            self.ent_cidade.focus_set()

    def _vaga_selecionada(self):
        sel = self.tree.selection()
        return self.resultados[int(sel[0])] if sel else None

    def _ao_selecionar(self, _evt=None):
        self._escrever_detalhe(self._vaga_selecionada())

    def _escrever_detalhe(self, v):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        if v is None:
            self.txt.insert("end", "Selecione um concurso na lista para ver os detalhes.\n"
                                   "Dê dois cliques para abrir a página do edital no navegador.")
            for b in (self.btn_det, self.btn_ins, self.btn_cp):
                b.config(state="disabled")
        else:
            self.txt.insert("end", f"{v.orgao}\n", "titulo")
            if v.titulo and v.titulo != v.orgao:
                self.txt.insert("end", f"{v.titulo}\n")

            def linha(rotulo, valor, tag=None):
                if valor:
                    self.txt.insert("end", f"{rotulo}: ", "rotulo")
                    self.txt.insert("end", f"{valor}\n", tag or ())

            vagas = "Cadastro de reserva" if v.vagas == "CR" else (f"{v.vagas} vaga(s)" if v.vagas else "")
            linha("Local", bc.UFS.get(v.uf, v.uf))
            linha("Vagas", vagas)
            linha("Salário (até)", v.salario)
            linha("Nível", v.nivel)
            linha("Cargos", v.cargos if v.cargos != v.titulo else "")
            if v.prazo:
                dias = (v.prazo - date.today()).days
                quando = ("ENCERRADO" if dias < 0 else "termina HOJE" if dias == 0 else f"faltam {dias} dia(s)")
                extra = f"  [{v.obs}]" if v.obs else ""
                linha("Inscrições até", f"{v.prazo:%d/%m/%Y} ({quando}){extra}", "alerta" if dias <= 3 else None)
            else:
                linha("Observação", v.obs)
            linha("Fonte", v.fonte)
            self.btn_det.config(state="normal")
            self.btn_ins.config(state="normal" if v.url_inscricao else "disabled")
            self.btn_cp.config(state="normal")
        self.txt.config(state="disabled")

    def abrir_detalhes(self):
        v = self._vaga_selecionada()
        if v:
            webbrowser.open(v.url)

    def abrir_inscricao(self):
        v = self._vaga_selecionada()
        if v and v.url_inscricao:
            webbrowser.open(v.url_inscricao)

    def copiar_link(self):
        v = self._vaga_selecionada()
        if v:
            self.root.clipboard_clear()
            self.root.clipboard_append(v.url_inscricao or v.url)
            self.lbl_status.config(text="Link copiado para a área de transferência.")

    # ------------------------------------------------------- Tabela/ordem
    @staticmethod
    def _chave(col, v):
        if col in ("prazo", "dias"):
            return (v.prazo is None, v.prazo or date.max)
        if col == "vagas":
            if v.vagas == "CR":
                return -1.0
            return float(v.vagas.replace(".", "")) if v.vagas else -2.0
        if col == "salario":
            m = re.search(r"[\d.]+(?:,\d+)?", v.salario or "")
            return float(m.group(0).replace(".", "").replace(",", ".")) if m else -1.0
        texto = {"cargos": v.cargos or v.titulo}.get(col, getattr(v, col, "") or "")
        return bc.normalizar(texto)

    def ordenar_por(self, col):
        if self.ordem_col == col:
            self.ordem_inv = not self.ordem_inv
        else:
            self.ordem_col, self.ordem_inv = col, col in ("vagas", "salario")
        self.renderizar()

    def renderizar(self):
        self.tree.delete(*self.tree.get_children())
        filtro = bc.normalizar(self.var_filtro.get())
        hoje = date.today()
        itens = [(i, v) for i, v in enumerate(self.resultados)
                 if not filtro or filtro in bc.normalizar(f"{v.texto} {v.cargos} {v.uf}")]
        itens.sort(key=lambda p: self._chave(self.ordem_col, p[1]), reverse=self.ordem_inv)

        for i, v in itens:
            dias = (v.prazo - hoje).days if v.prazo else None
            tags = ()
            if dias is not None:
                tags = ("encerrado",) if dias < 0 else ("hoje",) if dias == 0 else ("urgente",) if dias <= 7 else ()
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                v.prazo.strftime("%d/%m/%Y") if v.prazo else "—",
                "—" if dias is None else ("hoje" if dias == 0 else str(dias)),
                v.uf or "—", v.orgao, v.cargos or v.titulo,
                ("CR" if v.vagas == "CR" else v.vagas) or "—", v.salario or "—", v.nivel or "—", v.fonte,
            ))

        for cid, titulo, *_ in COLUNAS:
            seta = (" ▼" if self.ordem_inv else " ▲") if cid == self.ordem_col else ""
            self.tree.heading(cid, text=titulo + seta)
        self.lbl_contagem.config(
            text=f"{len(itens)} exibido(s) de {len(self.resultados)}  •  "
                 f"laranja = encerra em até 7 dias, vermelho = hoje")
        self._visiveis = [v for _, v in itens]
        self._escrever_detalhe(None)

    # --------------------------------------------------------------- Busca
    def iniciar_busca(self):
        if self.buscando:
            return
        tipo = self.var_tipo.get().strip()
        if not tipo:
            messagebox.showwarning("Faltou o tipo", "Digite o tipo de concurso (ex.: policia, saude, professor).")
            return
        fontes = [k for k, var in self.var_fontes.items() if var.get()]
        if not fontes:
            messagebox.showwarning("Nenhuma fonte", "Marque pelo menos uma fonte (PCI Concursos ou Atlas Público).")
            return
        est = self.var_uf.get()
        uf = None if est.startswith("Brasil") else est.split(" - ")[0]
        cidade = (self.var_cidade.get().strip() or None) if uf else None
        try:
            paginas = max(1, int(self.var_paginas.get()))
        except (tk.TclError, ValueError):
            paginas = 5

        self.buscando = True
        self.btn_buscar.config(state="disabled")
        self.btn_csv.config(state="disabled")
        self.progress.start(12)
        local = cidade + "/" + uf if cidade else (uf or "Brasil inteiro")
        self.lbl_status.config(text=f"Buscando '{tipo}' em {local}…")

        args = (tipo, uf, cidade, fontes, paginas, self.var_vencidos.get(), False)
        threading.Thread(target=self._trabalhar, args=(args,), daemon=True).start()
        self.root.after(100, self._verificar_fila)

    def _trabalhar(self, args):
        log = _FilaLog(self.fila)
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                res = bc.buscar(*args)
            self.fila.put(("ok", res))
        except Exception as e:  # noqa: BLE001
            self.fila.put(("erro", str(e)))

    def _verificar_fila(self):
        terminou = False
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "log":
                    self.lbl_status.config(text=dado[:140])
                elif tipo == "ok":
                    self.resultados = dado
                    self.ordem_col, self.ordem_inv = "prazo", False
                    self.renderizar()
                    n = len(dado)
                    self.lbl_status.config(
                        text=f"{n} concurso(s) encontrado(s)." if n else
                        "Nenhum concurso encontrado. Tente outro termo, mais páginas ou 'Incluir encerrados'.")
                    self.btn_csv.config(state="normal" if n else "disabled")
                    terminou = True
                elif tipo == "erro":
                    messagebox.showerror("Erro na busca", dado)
                    self.lbl_status.config(text="Erro na busca.")
                    terminou = True
        except queue.Empty:
            pass

        if terminou:
            self.buscando = False
            self.progress.stop()
            self.btn_buscar.config(state="normal")
        else:
            self.root.after(100, self._verificar_fila)

    # -------------------------------------------------------------- Export
    def exportar_csv(self):
        dados = getattr(self, "_visiveis", self.resultados)
        if not dados:
            return
        caminho = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")], initialfile="concursos.csv")
        if caminho:
            bc.salvar_csv(dados, caminho)
            self.lbl_status.config(text=f"{len(dados)} concurso(s) salvos em {caminho}")


def main():
    try:  # nitidez em telas de alta resolução no Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
