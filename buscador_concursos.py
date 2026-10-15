#!/usr/bin/env python3
"""
Buscador de concursos públicos brasileiros (editais com cargo, vagas, salário e prazo).

Fontes:
  pci    -> PCI Concursos      (órgão, cargos, vagas, salário, escolaridade, prazo)
  atlas  -> Atlas Público      (concursos e seletivos dos diários oficiais + link de inscrição)

Uso interativo:   python buscador_concursos.py
Uso por comando:  python buscador_concursos.py -t policia -e SC -c Joinville
                  python buscador_concursos.py -t "policia, bombeiro"      (Brasil inteiro)
                  python buscador_concursos.py -t saude -e SC --fontes pci --csv saida.csv

Dependências:  python -m pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
PCI = "https://www.pciconcursos.com.br"
ATLAS = "https://atlaspublico.com.br"
HEADERS = {"User-Agent": "Mozilla/5.0 (BuscadorConcursos/2.0; uso pessoal)"}
PAUSA = 1.0  # segundos entre requisições (seja gentil com os sites)

UFS = {
    "AC": "Acre", "AL": "Alagoas", "AM": "Amazonas", "AP": "Amapá", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MG": "Minas Gerais", "MS": "Mato Grosso do Sul",
    "MT": "Mato Grosso", "PA": "Pará", "PB": "Paraíba", "PE": "Pernambuco",
    "PI": "Piauí", "PR": "Paraná", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RO": "Rondônia", "RR": "Roraima", "RS": "Rio Grande do Sul", "SC": "Santa Catarina",
    "SE": "Sergipe", "SP": "São Paulo", "TO": "Tocantins",
}

# Termo digitado -> palavras que contam como "match"
SINONIMOS = {
    "policia": ["policia", "policial", "pm", "pc", "pf", "prf", "delegado", "investigador",
                "escrivao", "papiloscopista", "perito", "agente penitenciario", "policia penal",
                "policial penal", "guarda civil", "guarda municipal", "gcm", "soldado",
                "bombeiro", "bombeiros", "seguranca publica", "agente de policia"],
    "saude": ["saude", "enfermagem", "enfermeiro", "enfermeira", "medico", "medica", "hospital",
              "sus", "odontologo", "dentista", "farmaceutico", "agente comunitario", "samu",
              "psicologo", "fisioterapeuta", "nutricionista"],
    "educacao": ["professor", "professores", "professora", "educacao", "magisterio", "escola",
                 "pedagogo", "auxiliar de sala", "monitor"],
    "tribunal": ["tribunal", "tj", "trt", "trf", "tre", "tce", "mp", "ministerio publico",
                 "defensoria", "juiz", "analista judiciario", "tecnico judiciario"],
    "ti": ["ti", "tecnologia da informacao", "analista de sistemas", "desenvolvedor",
           "informatica", "analista de suporte"],
    "militar": ["exercito", "marinha", "aeronautica", "militar", "pm", "bombeiro", "soldado"],
    "banco": ["banco", "caixa economica", "bb", "bnb", "banrisul", "bancario", "escriturario"],
}


@dataclass
class Vaga:
    fonte: str
    orgao: str
    titulo: str
    uf: str
    vagas: str          # "12", "CR" (cadastro de reserva) ou ""
    salario: str
    cargos: str
    nivel: str
    prazo: date | None  # fim das inscrições
    obs: str
    url: str
    url_inscricao: str = ""
    texto: str = ""     # texto completo do card (usado nos filtros)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
RE_DATA = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
RE_VAGAS = re.compile(r"\b(\d[\d.]*)\s+vagas?\b", re.I)
RE_CR = re.compile(r"cadastro\s+(?:de\s+)?reserva", re.I)
RE_SALARIO = re.compile(r"R\$\s*[\d.]+(?:,\d{2})?(?:\s+por\s+hora)?", re.I)
RE_PRAZO_ATLAS = re.compile(r"inscri[cç][oõ]es\s+at[eé]\s+(\d{2})/(\d{2})/(\d{4})", re.I)
RE_LINHA_RUIDO = re.compile(
    r"R\$|\bvagas?\b|reserva|prorrogad|reabert|apenas\s+dia|apostila|^at[eé]$|^\d{2}/\d{2}/\d{4}$",
    re.I,
)


def normalizar(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", txt.lower()).strip()


def slugify(txt: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalizar(txt)).strip("-")


def mk_data(d, m, y):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def baixar(sessao, url: str, debug: bool = False):
    """Retorna bytes do HTML ou None (404/erro)."""
    for tentativa in range(3):
        try:
            r = sessao.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(PAUSA)
            if debug:
                pasta = Path("debug_html")
                pasta.mkdir(exist_ok=True)
                (pasta / f"{slugify(url)[-80:]}.html").write_bytes(r.content)
            return r.content
        except requests.RequestException as e:
            if tentativa == 2:
                print(f"  [aviso] falha ao acessar {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (tentativa + 1))
    return None


def linha_do_card(a, padrao):
    """Sobe na árvore HTML até o maior bloco que contém só este edital."""
    atual = a
    while atual.parent is not None and atual.parent.name not in ("body", "html", "[document]"):
        pai = atual.parent
        hrefs = {x["href"].split("#")[0] for x in pai.find_all("a", href=padrao)}
        if len(hrefs) > 1:
            break
        atual = pai
    if len(atual.get_text()) > 2500:  # subiu demais (ex.: página com 1 resultado só)
        return a.parent
    return atual


def linhas_de(no) -> list[str]:
    return [l.strip() for l in no.get_text("\n").split("\n") if l.strip()]


def proxima_pagina(soup, url_atual: str):
    a = soup.find("a", rel="next", href=True)
    if not a:
        for x in soup.find_all("a", href=True):
            t = x.get_text(" ", strip=True).lower()
            if t.startswith("próxim") or t in {"›", "»", ">", "next"}:
                a = x
                break
    if not a:
        return None
    prox = urljoin(url_atual, a["href"])
    return prox if prox != url_atual else None


# --------------------------------------------------------------------------
# Filtros
# --------------------------------------------------------------------------
def montar_matcher(tipos: str):
    """'policia, bombeiro' -> função(texto) True se casar com QUALQUER um dos termos."""
    matchers = []
    for tipo in [t for t in tipos.split(",") if t.strip()]:
        q = normalizar(tipo)
        if q in SINONIMOS:
            padrao = re.compile(r"\b(" + "|".join(re.escape(t) for t in SINONIMOS[q]) + r")\b")
            matchers.append(lambda txt, p=padrao: bool(p.search(normalizar(txt))))
        else:
            palavras = [p for p in q.split() if len(p) > 1]
            matchers.append(
                lambda txt, ps=palavras: all(re.search(rf"\b{re.escape(p)}", normalizar(txt)) for p in ps)
            )
    return lambda txt: any(m(txt) for m in matchers)


# --------------------------------------------------------------------------
# Fonte 1: PCI Concursos
# --------------------------------------------------------------------------
PADRAO_PCI = re.compile(r"/noticias/[^/?#]+/?$")


def parse_pci(html: bytes) -> list[Vaga]:
    soup = BeautifulSoup(html, "html.parser")
    achados, vistos = [], set()
    for a in soup.find_all("a", href=PADRAO_PCI):
        href = urljoin(PCI + "/", a["href"]).split("#")[0]
        orgao = a.get_text(" ", strip=True)
        if not orgao or href in vistos:
            continue
        vistos.add(href)

        titulo = a.get("title") or orgao
        card = linha_do_card(a, PADRAO_PCI)
        linhas = linhas_de(card)
        corpo = [l for l in linhas if l != orgao]
        texto = " | ".join(linhas)

        uf = next((l for l in corpo if re.fullmatch(r"[A-Z]{2}", l)), "")
        m_v = RE_VAGAS.search(" ".join(corpo))
        vagas = m_v.group(1) if m_v else ("CR" if RE_CR.search(texto) else "")
        m_s = RE_SALARIO.search(" ".join(corpo))
        salario = m_s.group(0) if m_s else ""

        datas = RE_DATA.findall(" ".join(corpo))
        prazo = mk_data(datas[-1][0], datas[-1][1], datas[-1][2]) if datas else None
        obs = ""
        for palavra in ("Prorrogado", "Reaberto", "Apenas dia"):
            if re.search(palavra, texto, re.I):
                obs = palavra
                break

        candidatos = [l for l in corpo if l != uf and not RE_LINHA_RUIDO.search(l)]
        cargos = candidatos[0] if candidatos else ""
        nivel = candidatos[1] if len(candidatos) > 1 else ""

        achados.append(Vaga("PCI", orgao, titulo, uf, vagas, salario, cargos, nivel,
                            prazo, obs, href, "", f"{orgao} {titulo} {cargos} {nivel}"))
    return achados


def buscar_pci(uf, cidade, max_pag, debug) -> list[Vaga]:
    sessao = requests.Session()
    filtrar_cidade = None
    if not uf:
        url = f"{PCI}/concursos/"
    else:
        url = f"{PCI}/concursos/{uf.lower()}/"
        if cidade:
            url_cidade = f"{PCI}/concursos/{uf.lower()}/{slugify(cidade)}"
            if baixar(sessao, url_cidade, debug):
                url = url_cidade
            else:
                filtrar_cidade = cidade  # sem página da cidade: filtra pelo nome depois
    print(f"[PCI] {url}")

    todos: dict[str, Vaga] = {}
    for n in range(max_pag):
        html = baixar(sessao, url, debug)
        if not html:
            break
        itens = parse_pci(html)
        if not itens:
            print(f"  [aviso] nenhum edital reconhecido em {url}. "
                  f"O layout pode ter mudado; rode com --debug e me envie o HTML salvo.")
            break
        novos = [v for v in itens if v.url not in todos]
        todos.update({v.url: v for v in novos})
        print(f"  página {n + 1}: {len(itens)} editais ({len(novos)} novos)")
        if not novos:
            break
        prox = proxima_pagina(BeautifulSoup(html, "html.parser"), url)
        if not prox:
            break
        url = prox

    res = list(todos.values())
    if filtrar_cidade:
        c = normalizar(filtrar_cidade)
        res = [v for v in res if c in normalizar(v.texto)]
    return res


# --------------------------------------------------------------------------
# Fonte 2: Atlas Público
# --------------------------------------------------------------------------
PADRAO_ATLAS = re.compile(r"/concursos/[a-z]{2}/[^/?#]+-\d+/?$")


def parse_atlas(html: bytes) -> list[Vaga]:
    soup = BeautifulSoup(html, "html.parser")
    achados, vistos = [], set()
    for a in soup.find_all("a", href=PADRAO_ATLAS):
        href = urljoin(ATLAS + "/", a["href"]).split("#")[0]
        orgao = a.get_text(" ", strip=True)
        if not orgao or orgao.lower().startswith("ver detalhes") or href in vistos:
            continue
        vistos.add(href)

        card = linha_do_card(a, PADRAO_ATLAS)
        linhas = linhas_de(card)
        texto = " | ".join(linhas)

        m_prazo = RE_PRAZO_ATLAS.search(texto)
        if m_prazo:
            prazo = mk_data(*m_prazo.groups())
        else:
            datas = RE_DATA.findall(texto)
            prazo = mk_data(datas[0][0], datas[0][1], datas[0][2]) if datas else None

        m_v = RE_VAGAS.search(texto)
        vagas = m_v.group(1) if m_v else ("CR" if RE_CR.search(texto) else "")
        m_s = RE_SALARIO.search(texto)
        salario = m_s.group(0) if m_s else ""
        m_b = re.search(r"banca\s+([^|·]+)", texto, re.I)
        banca = m_b.group(1).strip() if m_b else ""

        descricoes = [l for l in linhas if len(l) > 40 and l != orgao]
        descricao = max(descricoes, key=len) if descricoes else ""
        uf_m = re.search(r"/concursos/([a-z]{2})/", href)

        inscricao = ""
        for x in card.find_all("a", href=True):
            if "inscreva" in x.get_text(" ", strip=True).lower():
                inscricao = urljoin(ATLAS + "/", x["href"])
                break

        achados.append(Vaga("Atlas", orgao, descricao or orgao, uf_m.group(1).upper() if uf_m else "",
                            vagas, salario, descricao, "", prazo,
                            f"banca {banca}" if banca else "", href, inscricao, texto))
    return achados


def buscar_atlas(uf, cidade, max_pag, debug) -> list[Vaga]:
    sessao = requests.Session()
    base = f"{ATLAS}/concursos/{uf.lower()}" if uf else f"{ATLAS}/cadernos/concursos"
    print(f"[Atlas] {base}")

    todos: dict[str, Vaga] = {}
    for n in range(1, max_pag + 1):
        url = base if n == 1 else f"{base}?page={n}"
        html = baixar(sessao, url, debug)
        if not html:
            break
        itens = parse_atlas(html)
        novos = [v for v in itens if v.url not in todos]
        if not novos:
            if n == 1:
                print("  [aviso] nenhum concurso reconhecido no Atlas (layout mudou ou página vazia).")
            break
        todos.update({v.url: v for v in novos})
        print(f"  página {n}: {len(itens)} concursos ({len(novos)} novos)")

    res = list(todos.values())
    if cidade:
        c = normalizar(cidade)
        res = [v for v in res if c in normalizar(v.texto)]
    return res


FONTES = {"pci": buscar_pci, "atlas": buscar_atlas}


# --------------------------------------------------------------------------
# Busca principal
# --------------------------------------------------------------------------
def buscar(tipo, uf, cidade, fontes, max_pag, incluir_vencidos, debug) -> list[Vaga]:
    brutos: list[Vaga] = []
    for nome in fontes:
        try:
            brutos += FONTES[nome](uf, cidade, max_pag, debug)
        except Exception as e:  # uma fonte com problema não derruba as outras
            print(f"  [aviso] fonte '{nome}' falhou: {e}", file=sys.stderr)

    casa = montar_matcher(tipo)
    hoje = date.today()
    res = []
    for v in brutos:
        if not casa(v.texto):
            continue
        if uf and v.uf and v.uf.upper() not in (uf.upper(), "BR"):
            continue
        if not incluir_vencidos and v.prazo and v.prazo < hoje:
            continue
        res.append(v)

    res.sort(key=lambda v: (v.prazo is None, v.prazo or date.max))
    return res


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
def pedir_uf():
    while True:
        v = input("Estado (sigla, ex: SC) [Enter = Brasil inteiro]: ").strip().upper()
        if not v:
            return None
        if v in UFS:
            return v
        print("  Sigla inválida. Use algo como SP, RJ, SC...")


def mostrar(res: list[Vaga]):
    if not res:
        print("\nNenhum concurso encontrado. Tente outro termo, aumente --paginas ou use --vencidos.")
        return
    hoje = date.today()
    print(f"\n{len(res)} concurso(s) encontrado(s) (ordenados pelo fim das inscrições):\n")
    for i, v in enumerate(res, 1):
        print(f"{i:>3}. [{v.uf or '--'}] {v.orgao}   ({v.fonte})")
        if v.titulo and v.titulo != v.orgao:
            print(f"     {v.titulo}")
        partes = []
        if v.vagas:
            partes.append("Cadastro de reserva" if v.vagas == "CR" else f"{v.vagas} vaga(s)")
        if v.salario:
            partes.append(v.salario)
        if v.nivel:
            partes.append(v.nivel)
        if partes:
            print("     " + "  |  ".join(partes))
        if v.cargos and v.cargos != v.titulo:
            print(f"     Cargos: {v.cargos}")
        if v.prazo:
            dias = (v.prazo - hoje).days
            quando = ("encerrado" if dias < 0 else "termina hoje" if dias == 0
                      else f"faltam {dias} dia(s)")
            extra = f" [{v.obs}]" if v.obs else ""
            print(f"     Inscrições até {v.prazo:%d/%m/%Y} ({quando}){extra}")
        elif v.obs:
            print(f"     {v.obs}")
        print(f"     Detalhes:  {v.url}")
        if v.url_inscricao:
            print(f"     Inscrição: {v.url_inscricao}")
        print()


def salvar_csv(res, caminho):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fonte", "orgao", "titulo", "uf", "vagas", "salario", "cargos", "nivel",
                    "inscricoes_ate", "obs", "url", "url_inscricao"])
        for v in res:
            w.writerow([v.fonte, v.orgao, v.titulo, v.uf, v.vagas, v.salario, v.cargos, v.nivel,
                        v.prazo.strftime("%d/%m/%Y") if v.prazo else "", v.obs, v.url, v.url_inscricao])
    print(f"Salvo em {caminho}")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="Buscador de concursos públicos")
    p.add_argument("-t", "--tipo", help='tipo de concurso (ex: policia, saude, "professor, enfermeiro")')
    p.add_argument("-e", "--estado", help="sigla do estado (vazio = Brasil inteiro)")
    p.add_argument("-c", "--cidade", help="cidade (opcional)")
    p.add_argument("--fontes", default="pci,atlas", help="fontes separadas por vírgula: pci,atlas")
    p.add_argument("--paginas", type=int, default=5, help="máximo de páginas por fonte (padrão 5)")
    p.add_argument("--vencidos", action="store_true", help="inclui concursos com inscrição encerrada")
    p.add_argument("--csv", help="salva o resultado neste arquivo CSV")
    p.add_argument("--debug", action="store_true", help="salva o HTML baixado em ./debug_html")
    args = p.parse_args()

    fontes = [f.strip().lower() for f in args.fontes.split(",") if f.strip()]
    invalidas = [f for f in fontes if f not in FONTES]
    if invalidas:
        sys.exit(f"Fonte(s) desconhecida(s): {', '.join(invalidas)}. Opções: {', '.join(FONTES)}")

    if args.tipo:
        tipo, uf, cidade = args.tipo, (args.estado or "").upper() or None, args.cidade
        if uf and uf not in UFS:
            sys.exit(f"Estado inválido: {uf}")
        if cidade and not uf:
            sys.exit("Para filtrar por cidade, informe também o estado (-e).")
    else:
        tipo = input("Que tipo de concurso você procura? (ex: policia, saude, professor): ").strip()
        if not tipo:
            sys.exit("Informe um tipo de concurso.")
        uf = pedir_uf()
        cidade = (input("Cidade [Enter = todas]: ").strip() or None) if uf else None

    print()
    res = buscar(tipo, uf, cidade, fontes, args.paginas, args.vencidos, args.debug)
    mostrar(res)
    if args.csv and res:
        salvar_csv(res, args.csv)


if __name__ == "__main__":
    main()
