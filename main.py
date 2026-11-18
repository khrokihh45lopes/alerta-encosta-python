"""
Alerta Encosta - O Cérebro Conectado
Computational Thinking with Python - FIAP Global Solution 2026.2

Busca no ThingSpeak as leituras que o ESP32 publicou (umidade do solo e
inclinação do terreno), cruza as duas medidas num Índice de Risco de
deslizamento, percebe quando a situação está piorando e mantém um
histórico sem repetir leituras.

Como rodar:  python main.py
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Apelidos de tipo: deixam claro o que cada função recebe e devolve.
Config = dict[str, Any]   # o dicionário CONFIG (limites, pesos, avisos)
Feed = dict[str, Any]     # entrada crua do ThingSpeak: entry_id, created_at, field1, field2...
Leitura = dict[str, Any]  # nossa leitura: {id, horario, medidas, risco, faixa}

# ----------------------------------------------------------------------
# Arquivos (sempre ao lado deste main.py, de onde quer que se rode)
# ----------------------------------------------------------------------
PASTA = Path(__file__).resolve().parent
ARQ_BACKUP = PASTA / "backup.json"
ARQ_HISTORICO = PASTA / "historico.json"
ARQ_RELATORIO = PASTA / "relatorio.csv"

FUSO_RIO = timezone(timedelta(hours=-3))
FAIXAS = ("NORMAL", "ATENÇÃO", "PERIGO")

# ----------------------------------------------------------------------
# Configuração: limites, pesos e avisos
# Os MESMOS valores estão no ESP32 (esp32/sketch.ino) e no site
# (web/src/domain/risco.js). Justificativa no README desta pasta.
# ----------------------------------------------------------------------
CONFIG: Config = {
    "nome": "Alerta Encosta",
    "local": "Rio de Janeiro",  # TODO equipe: troque pelo bairro/encosta real
    "thingspeak": {
        "canal_id": "3584820",  # canal público da equipe no ThingSpeak
        "chave_leitura": "",  # Read API Key; deixe vazio se o canal for público
        "resultados": 30,  # quantas leituras buscar (mínimo 20)
        "timeout_s": 8,
    },
    "medidas": {
        "umidade_solo": {
            "campo": "field1",
            "rotulo": "Umidade do solo",
            "unidade": "%",
            "min": 30.0,
            "max": 90.0,
            "peso": 0.6,
            "limite_fisico": (0.0, 100.0),  # valores possíveis do sensor
        },
        "inclinacao": {
            "campo": "field2",
            "rotulo": "Inclinação do terreno",
            "unidade": "°",
            "min": 0.0,
            "max": 10.0,
            "peso": 0.4,
            "limite_fisico": (0.0, 90.0),
        },
    },
    "limites_faixa": {"ATENÇÃO": 40.0, "PERIGO": 70.0},
    "altas_para_piorando": 3,
    "avisos": {
        "NORMAL": "Sem sinal de risco agora. Mantenha valas e calhas limpas e não jogue lixo na encosta.",
        "ATENÇÃO": "Solo encharcado. Separe documentos, remédios e lanterna e fique atento à sirene.",
        "PERIGO": "Risco de deslizamento: saia de casa e vá para o ponto de apoio. Defesa Civil: 199.",
        "PIORANDO": "A situação está piorando: não espere a sirene para se preparar.",
    },
}


# ======================================================================
# 1. Configuração
# ======================================================================
def validar_config(config: Config) -> None:
    """Confere se a configuração faz sentido; se não fizer, dispara ValueError.

    Regras: pelo menos 2 medidas, mínimo < máximo em cada medida, pesos
    somando 1 e limite de ATENÇÃO menor que o de PERIGO.
    """
    medidas = config["medidas"]
    if len(medidas) < 2:
        raise ValueError("O índice precisa de pelo menos 2 medidas.")
    for nome, m in medidas.items():
        if m["min"] >= m["max"]:
            raise ValueError(f"'{nome}': mínimo ({m['min']}) deve ser menor que o máximo ({m['max']}).")
    soma_pesos = sum(m["peso"] for m in medidas.values())
    if abs(soma_pesos - 1.0) > 1e-6:
        raise ValueError(f"Os pesos precisam somar 1 (estão somando {soma_pesos:.2f}).")
    limites = config["limites_faixa"]
    if not 0 < limites["ATENÇÃO"] < limites["PERIGO"] <= 100:
        raise ValueError("Limites das faixas inválidos: use 0 < ATENÇÃO < PERIGO <= 100.")


# ======================================================================
# 2. Arquivos JSON (nada derruba: arquivo ausente ou corrompido)
# ======================================================================
def carregar_json(caminho: Path, padrao: object) -> object:
    """Lê um arquivo JSON. Se não existir ou estiver corrompido, devolve `padrao`."""
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except FileNotFoundError:
        return padrao
    except (json.JSONDecodeError, OSError) as erro:
        print(f"  [aviso] Não consegui ler {caminho.name} ({erro}). Seguindo sem ele.")
        return padrao


def salvar_json(caminho: Path, dados: object) -> bool:
    """Grava `dados` em JSON. Devolve True se deu certo."""
    try:
        with open(caminho, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo, ensure_ascii=False, indent=2)
        return True
    except OSError as erro:
        print(f"  [aviso] Não consegui salvar {caminho.name} ({erro}).")
        return False


# ======================================================================
# 3. API do ThingSpeak + plano B
# ======================================================================
def canal_configurado(config: Config) -> bool:
    """Diz se a equipe já preencheu o número do canal do ThingSpeak."""
    return str(config["thingspeak"]["canal_id"]).strip().isdigit()


def buscar_feeds_thingspeak(config: Config) -> list[Feed]:
    """Busca as últimas leituras do canal. Pode lançar requests.RequestException."""
    ts = config["thingspeak"]
    url = f"https://api.thingspeak.com/channels/{ts['canal_id']}/feeds.json"
    parametros = {"results": max(20, ts["resultados"])}
    if ts["chave_leitura"]:
        parametros["api_key"] = ts["chave_leitura"]
    resposta = requests.get(url, params=parametros, timeout=ts["timeout_s"])
    if resposta.status_code != 200:
        raise requests.HTTPError(f"ThingSpeak respondeu com status {resposta.status_code}")
    corpo = resposta.json()
    if not isinstance(corpo, dict) or "feeds" not in corpo:
        raise requests.RequestException("Resposta do ThingSpeak sem a lista 'feeds' (canal privado?)")
    return corpo["feeds"]


def salvar_backup(feeds: list[Feed]) -> None:
    """Guarda a última busca que deu certo, para usar quando não houver rede."""
    dados = {
        "fonte": "ThingSpeak (última busca que deu certo)",
        "salvo_em": datetime.now(FUSO_RIO).isoformat(timespec="seconds"),
        "feeds": feeds,
    }
    salvar_json(ARQ_BACKUP, dados)


def carregar_backup() -> list[Feed]:
    """Lê as leituras guardadas no backup.json (plano B)."""
    dados = carregar_json(ARQ_BACKUP, {})
    if isinstance(dados, dict) and isinstance(dados.get("feeds"), list):
        return dados["feeds"]
    return []


def obter_feeds(config: Config) -> tuple[list[Feed], str]:
    """Tenta o ThingSpeak; se falhar, usa o backup. Devolve (feeds, origem)."""
    if not canal_configurado(config):
        print("  [aviso] Canal do ThingSpeak não configurado em CONFIG. Usando backup.json.")
        return carregar_backup(), "backup.json"
    try:
        feeds = buscar_feeds_thingspeak(config)
    except requests.RequestException as erro:
        print(f"  [aviso] Sem acesso ao ThingSpeak ({erro}). Usando backup.json.")
        return carregar_backup(), "backup.json"
    leituras, _ = converter_feeds(feeds, config)
    if not leituras:  # canal novo ou só com campos vazios: não apagar o backup bom
        print("  [aviso] O canal do ThingSpeak ainda não tem leituras válidas. Usando backup.json.")
        return carregar_backup(), "backup.json"
    salvar_backup(feeds)
    return feeds, "ThingSpeak"


# ======================================================================
# 4. Modelo de dados: feed do ThingSpeak -> leitura
# ======================================================================
def converter_horario(texto: str) -> datetime:
    """Converte '2026-11-04T13:30:00Z' (UTC) para a hora do Rio."""
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(FUSO_RIO)


def ler_medidas(feed: Feed, config: Config) -> dict[str, float] | None:
    """Extrai as medidas do feed. Devolve None se algum campo estiver vazio ou inválido."""
    medidas: dict[str, float] = {}
    for nome, m in config["medidas"].items():
        try:
            medidas[nome] = float(feed[m["campo"]])
        except (KeyError, TypeError, ValueError):
            return None
    return medidas


def calcular_nivel(valor: float, minimo: float, maximo: float) -> float:
    """nivel = (valor - min) / (max - min) * 100, limitado entre 0 e 100."""
    nivel = (valor - minimo) / (maximo - minimo) * 100
    return max(0.0, min(100.0, nivel))


def calcular_risco(medidas: dict[str, float], config: Config) -> float:
    """risco = soma de peso * nivel de cada medida (0 a 100)."""
    risco = 0.0
    for nome, valor in medidas.items():
        m = config["medidas"][nome]
        risco += m["peso"] * calcular_nivel(valor, m["min"], m["max"])
    return round(risco, 1)


def classificar_faixa(risco: float, config: Config) -> str:
    """NORMAL abaixo de 40, ATENÇÃO de 40 a 70, PERIGO a partir de 70."""
    limites = config["limites_faixa"]
    if risco >= limites["PERIGO"]:
        return "PERIGO"
    if risco >= limites["ATENÇÃO"]:
        return "ATENÇÃO"
    return "NORMAL"


def criar_leitura(feed: Feed, config: Config) -> Leitura | None:
    """Monta o dicionário {id, horario, medidas, risco, faixa}. None se o feed for inválido."""
    medidas = ler_medidas(feed, config)
    if medidas is None:
        return None
    try:
        id_leitura = int(feed["entry_id"])
        horario = converter_horario(feed["created_at"])
    except (KeyError, TypeError, ValueError):  # campo faltando, vazio ou com texto errado
        return None
    risco = calcular_risco(medidas, config)
    return {
        "id": id_leitura,
        "horario": horario.isoformat(timespec="seconds"),
        "medidas": medidas,
        "risco": risco,
        "faixa": classificar_faixa(risco, config),
    }


def converter_feeds(feeds: list[Feed], config: Config) -> tuple[list[Leitura], int]:
    """Converte todos os feeds. Devolve (leituras válidas em ordem, quantas foram descartadas)."""
    leituras: list[Leitura] = []
    descartadas = 0
    for feed in feeds:
        leitura = criar_leitura(feed, config)
        if leitura is None:
            descartadas += 1
        else:
            leituras.append(leitura)
    leituras.sort(key=lambda leitura: leitura["id"])
    return leituras, descartadas


# ======================================================================
# 5. Memória: histórico sem duplicar
# ======================================================================
def leitura_valida(item: object, config: Config) -> bool:
    """Confere se um item lido do historico.json tem o formato de uma Leitura.

    Protege o programa de um histórico editado à mão ou salvo por uma versão
    antiga, com outras medidas.
    """
    if not isinstance(item, dict):
        return False
    try:
        datetime.fromisoformat(item["horario"])
        float(item["risco"])
        return (
            isinstance(item["id"], int)
            and isinstance(item["medidas"], dict)
            and set(item["medidas"]) == set(config["medidas"])
            and item["faixa"] in FAIXAS
        )
    except (KeyError, TypeError, ValueError):
        return False


def carregar_historico(config: Config) -> list[Leitura]:
    """Lê o historico.json, ignorando itens inválidos. Lista vazia se não existir."""
    dados = carregar_json(ARQ_HISTORICO, [])
    if not isinstance(dados, list):
        return []
    validas = [item for item in dados if leitura_valida(item, config)]
    ignoradas = len(dados) - len(validas)
    if ignoradas:
        print(f"  [aviso] {ignoradas} item(ns) inválido(s) no {ARQ_HISTORICO.name} foram ignorados.")
    return validas


def mesclar_historico(historico: list[Leitura], novas: list[Leitura]) -> tuple[list[Leitura], int]:
    """Junta as leituras novas ao histórico sem repetir horário. Devolve (histórico, quantas entraram).

    A leitura é reconhecida pelo horário, e não pelo entry_id, porque o ThingSpeak
    recomeça o entry_id do 1 quando o canal é limpo (Clear Channel) ou trocado.
    """
    horarios_conhecidos: set[str] = {leitura["horario"] for leitura in historico}
    adicionadas = 0
    for leitura in novas:
        if leitura["horario"] not in horarios_conhecidos:
            historico.append(leitura)
            horarios_conhecidos.add(leitura["horario"])
            adicionadas += 1
    historico.sort(key=lambda leitura: datetime.fromisoformat(leitura["horario"]))
    return historico, adicionadas


def exportar_csv(leituras: list[Leitura], config: Config, caminho: Path = ARQ_RELATORIO) -> bool:
    """Exporta as leituras para CSV com cabeçalho. Devolve True se deu certo."""
    nomes = list(config["medidas"].keys())
    try:
        with open(caminho, "w", newline="", encoding="utf-8") as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(["id", "horario", *nomes, "risco", "faixa"])
            for leitura in leituras:
                valores = [leitura["medidas"][nome] for nome in nomes]
                escritor.writerow([leitura["id"], leitura["horario"], *valores, leitura["risco"], leitura["faixa"]])
        return True
    except OSError as erro:
        print(f"  [aviso] Não consegui exportar o CSV ({erro}). O arquivo está aberto no Excel?")
        return False


# ======================================================================
# 6. Análise
# ======================================================================
def contar_altas_finais(leituras: list[Leitura]) -> int:
    """Conta quantas altas seguidas de risco existem no fim da série."""
    altas = 0
    for i in range(len(leituras) - 1, 0, -1):
        if leituras[i]["risco"] > leituras[i - 1]["risco"]:
            altas += 1
        else:
            break
    return altas


def contar_quedas_finais(leituras: list[Leitura]) -> int:
    """Conta quantas quedas seguidas de risco existem no fim da série."""
    quedas = 0
    for i in range(len(leituras) - 1, 0, -1):
        if leituras[i]["risco"] < leituras[i - 1]["risco"]:
            quedas += 1
        else:
            break
    return quedas


def analisar_tendencia(leituras: list[Leitura], minimo_altas: int) -> tuple[str, int]:
    """PIORANDO com N altas seguidas no fim, MELHORANDO com N quedas, senão ESTÁVEL."""
    altas = contar_altas_finais(leituras)
    if altas >= minimo_altas:
        return "PIORANDO", altas
    quedas = contar_quedas_finais(leituras)
    if quedas >= minimo_altas:
        return "MELHORANDO", quedas
    return "ESTÁVEL", 0


def encontrar_episodios(leituras: list[Leitura], faixa: str) -> list[list[Leitura]]:
    """Agrupa leituras seguidas na mesma faixa em episódios (listas de leituras)."""
    episodios: list[list[Leitura]] = []
    atual: list[Leitura] = []
    for leitura in leituras:
        if leitura["faixa"] == faixa:
            atual.append(leitura)
        elif atual:
            episodios.append(atual)
            atual = []
    if atual:
        episodios.append(atual)
    return episodios


def duracao_episodio(episodio: list[Leitura]) -> timedelta:
    """Tempo entre a primeira e a última leitura de um episódio."""
    inicio = datetime.fromisoformat(episodio[0]["horario"])
    fim = datetime.fromisoformat(episodio[-1]["horario"])
    return fim - inicio


def maior_episodio(episodios: list[list[Leitura]]) -> list[Leitura] | None:
    """Devolve o episódio mais longo (em tempo; empate desempata pelo nº de leituras)."""
    if not episodios:
        return None
    return max(episodios, key=lambda ep: (duracao_episodio(ep), len(ep)))


def contar_por_faixa(leituras: list[Leitura]) -> dict[str, int]:
    """Quantas leituras caíram em cada faixa."""
    contagem = {faixa: 0 for faixa in FAIXAS}
    for leitura in leituras:
        contagem[leitura["faixa"]] += 1
    return contagem


def leitura_de_pico(leituras: list[Leitura]) -> Leitura:
    """A leitura com o maior risco."""
    return max(leituras, key=lambda leitura: leitura["risco"])


def risco_medio(leituras: list[Leitura]) -> float:
    """Média do índice de risco."""
    return sum(leitura["risco"] for leitura in leituras) / len(leituras)


def piores_leituras(leituras: list[Leitura], quantidade: int = 5) -> list[Leitura]:
    """Ranking das leituras de maior risco (para ir além)."""
    return sorted(leituras, key=lambda leitura: leitura["risco"], reverse=True)[:quantidade]


def buscar_por_horario(historico: list[Leitura], alvo: datetime) -> Leitura | None:
    """Busca binária: devolve a leitura do histórico mais próxima do horário `alvo`.

    O histórico está ordenado por horário (`mesclar_historico` garante isso).
    Cada passo descarta metade da lista: 1.000 leituras pedem no
    máximo 10 comparações, em vez de olhar uma por uma.
    """
    if not historico:
        return None
    inicio, fim = 0, len(historico) - 1
    while inicio < fim:
        meio = (inicio + fim) // 2
        if datetime.fromisoformat(historico[meio]["horario"]) < alvo:
            inicio = meio + 1  # o alvo está na metade da direita
        else:
            fim = meio  # o alvo está no meio ou na metade da esquerda
    # `inicio` é a primeira leitura >= alvo; a anterior a ela pode estar mais perto
    candidatas = historico[max(0, inicio - 1): inicio + 1]
    return min(candidatas, key=lambda leitura: abs(datetime.fromisoformat(leitura["horario"]) - alvo))


def agrupar_por_dia(leituras: list[Leitura]) -> dict[str, list[Leitura]]:
    """Separa as leituras por dia. Chave 'AAAA-MM-DD' (o horário já está na hora do Rio)."""
    dias: dict[str, list[Leitura]] = {}
    for leitura in leituras:
        dia = leitura["horario"][:10]
        dias.setdefault(dia, []).append(leitura)
    return dias


def resumir_dia(leituras: list[Leitura]) -> dict[str, float]:
    """Risco médio, pico e nº de leituras em PERIGO de um dia."""
    return {
        "Risco médio": risco_medio(leituras),
        "Pico de risco": leitura_de_pico(leituras)["risco"],
        "Leituras em PERIGO": contar_por_faixa(leituras)["PERIGO"],
    }


def comparar_dias(historico: list[Leitura]) -> list[str]:
    """Compara o último dia do histórico com o dia anterior a ele (hoje x ontem)."""
    dias = agrupar_por_dia(historico)
    if len(dias) < 2:
        unico = ", ".join(formatar_dia(dia) for dia in dias) or "nenhum dia"
        return [f"O histórico só tem leituras de {unico}.",
                "A comparação aparece quando houver leituras de dois dias diferentes."]
    anterior, ultimo = sorted(dias)[-2:]
    resumo_anterior = resumir_dia(dias[anterior])
    resumo_ultimo = resumir_dia(dias[ultimo])
    linhas = [f"{'':20}{formatar_dia(anterior):>8}{formatar_dia(ultimo):>8}   variação"]
    for nome, valor_anterior in resumo_anterior.items():
        valor_ultimo = resumo_ultimo[nome]
        linhas.append(f"{nome:20}{valor_anterior:8.0f}{valor_ultimo:8.0f}   {valor_ultimo - valor_anterior:+.0f}")
    diferenca = resumo_ultimo["Risco médio"] - resumo_anterior["Risco médio"]
    if diferenca > 0:
        conclusao = f"PIOR que {formatar_dia(anterior)} (risco médio subiu {diferenca:.0f} pontos)"
    elif diferenca < 0:
        conclusao = f"MELHOR que {formatar_dia(anterior)} (risco médio caiu {-diferenca:.0f} pontos)"
    else:
        conclusao = f"igual a {formatar_dia(anterior)}"
    linhas.append(f"=> {formatar_dia(ultimo)} foi {conclusao}.")
    return linhas


# ======================================================================
# 7. Apresentação (textos para o cidadão / agente comunitário)
# ======================================================================
def formatar_hora(horario_iso: str) -> str:
    """'2026-11-03T18:30:00-03:00' -> '03/11 18:30'."""
    return datetime.fromisoformat(horario_iso).strftime("%d/%m %H:%M")


def formatar_dia(dia_iso: str) -> str:
    """'2026-11-03' -> '03/11'."""
    return f"{dia_iso[8:10]}/{dia_iso[5:7]}"


def formatar_duracao(duracao: timedelta) -> str:
    """timedelta -> '2d05h', '1h30', '40min' ou '45s'."""
    segundos = int(duracao.total_seconds())
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, segs = divmod(resto, 60)
    if dias:
        return f"{dias}d{horas:02d}h"
    if horas:
        return f"{horas}h{minutos:02d}"
    if minutos:
        return f"{minutos}min"
    return f"{segs}s"


def formatar_medidas(medidas: dict[str, float], config: Config) -> str:
    """{'umidade_solo': 72.4, ...} -> 'solo 72.4% · incl. 3.1°'."""
    partes = []
    for nome, valor in medidas.items():
        m = config["medidas"][nome]
        rotulo = m["rotulo"].split()[0].lower()
        partes.append(f"{rotulo} {valor:.1f}{m['unidade']}")
    return " · ".join(partes)


def montar_relatorio(leituras: list[Leitura], config: Config, titulo: str) -> list[str]:
    """Monta as linhas do relatório (risco, pico, episódios, tendência, aviso)."""
    if not leituras:
        return [f"=== {config['nome']} · {config['local']} ===", "Nenhuma leitura para analisar."]

    pico = leitura_de_pico(leituras)
    ultima = leituras[-1]
    episodios = encontrar_episodios(leituras, "PERIGO")
    maior = maior_episodio(episodios)
    tendencia, n = analisar_tendencia(leituras, config["altas_para_piorando"])
    contagem = contar_por_faixa(leituras)

    linhas = [
        f"=== {config['nome']} · {config['local']} ===",
        titulo,
        f"Agora ({formatar_hora(ultima['horario'])}): risco {ultima['risco']:.0f} · {ultima['faixa']}"
        f"  [{formatar_medidas(ultima['medidas'], config)}]",
        f"Risco médio {risco_medio(leituras):.0f} · pico {pico['risco']:.0f} às {formatar_hora(pico['horario'])}",
    ]
    if maior is None:
        linhas.append("PERIGO: nenhum episódio no período")
    else:
        linhas.append(
            f"PERIGO: {len(episodios)} episódio(s) · o maior durou {formatar_duracao(duracao_episodio(maior))}"
            f" ({len(maior)} leituras, de {formatar_hora(maior[0]['horario'])} a {formatar_hora(maior[-1]['horario'])})"
        )
    linhas.append(
        "Leituras por faixa: " + " · ".join(f"{faixa} {qtd}" for faixa, qtd in contagem.items())
    )
    detalhe = f" ({n} altas seguidas)" if tendencia == "PIORANDO" else f" ({n} quedas seguidas)" if n else ""
    linhas.append(f"Tendência: {tendencia}{detalhe}")
    linhas.append(f"AVISO: {config['avisos'][ultima['faixa']]}")
    if tendencia == "PIORANDO":
        linhas.append(f"AVISO: {config['avisos']['PIORANDO']}")
    return linhas


def imprimir_linhas(linhas: list[str]) -> None:
    """Mostra as linhas na tela com um espaço antes e depois."""
    print()
    for linha in linhas:
        print(linha)
    print()


# ======================================================================
# 8. Entrada do usuário (nada derruba: letras, vazio, Ctrl+C)
# ======================================================================
def ler_texto(mensagem: str) -> str | None:
    """input() que não quebra com Ctrl+C / Ctrl+D: nesses casos devolve None."""
    try:
        return input(mensagem).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def ler_numero(mensagem: str, minimo: float, maximo: float) -> float | None:
    """Pede um número entre minimo e maximo. Aceita vírgula. Enter vazio (ou Ctrl+C) devolve None."""
    while True:
        texto = ler_texto(mensagem)
        if not texto:
            return None
        try:
            valor = float(texto.replace(",", "."))
        except ValueError:
            print(f"  Digite só o número, de {minimo:g} a {maximo:g} (ex.: 72,5).")
            continue
        if minimo <= valor <= maximo:
            return valor
        print(f"  Valor fora do intervalo: use de {minimo:g} a {maximo:g}.")


def ler_horario(mensagem: str, ano: int) -> datetime | None:
    """Pede um horário no formato DD/MM HH:MM (hora do Rio). Enter vazio cancela."""
    while True:
        texto = ler_texto(mensagem)
        if not texto:
            return None
        try:
            horario = datetime.strptime(f"{texto} {ano}", "%d/%m %H:%M %Y")
        except ValueError:
            print("  Use o formato DD/MM HH:MM (ex.: 03/11 18:30).")
            continue
        return horario.replace(tzinfo=FUSO_RIO)


# ======================================================================
# 9. Ações do menu
# ======================================================================
def acao_buscar(estado: dict, config: Config) -> None:
    """Busca leituras (ThingSpeak ou backup), atualiza o histórico e mostra o relatório."""
    print("\n  Buscando leituras...")
    feeds, origem = obter_feeds(config)
    leituras, descartadas = converter_feeds(feeds, config)
    if not leituras:
        print("  Nenhuma leitura válida encontrada (nem no ThingSpeak, nem no backup).")
        return
    historico, novas = mesclar_historico(estado["historico"], leituras)
    estado["historico"] = historico
    salvar_json(ARQ_HISTORICO, historico)
    titulo = f"{origem}: {len(leituras)} leituras · {novas} novas · histórico {len(historico)}"
    if descartadas:
        titulo += f" · {descartadas} descartada(s) (campo vazio ou inválido)"
    imprimir_linhas(montar_relatorio(leituras, config, titulo))


def acao_relatorio_historico(estado: dict, config: Config) -> None:
    """Mostra o relatório do histórico inteiro."""
    historico = estado["historico"]
    titulo = f"Histórico completo: {len(historico)} leituras"
    imprimir_linhas(montar_relatorio(historico, config, titulo))


def acao_ranking(estado: dict, config: Config) -> None:
    """Mostra as 5 leituras de maior risco do histórico."""
    if not estado["historico"]:
        print("\n  Histórico vazio. Use a opção 1 primeiro.\n")
        return
    print("\n  As 5 piores leituras do histórico:")
    for posicao, leitura in enumerate(piores_leituras(estado["historico"]), start=1):
        print(f"  {posicao}. {formatar_hora(leitura['horario'])}  risco {leitura['risco']:5.1f}  {leitura['faixa']:8}"
              f"  [{formatar_medidas(leitura['medidas'], config)}]")
    print()


def acao_simular(config: Config) -> None:
    """O usuário digita os valores e vê o risco calculado (testa a fórmula sem sensor)."""
    print("\n  Simular uma leitura (Enter vazio cancela)")
    medidas: dict[str, float] = {}
    for nome, m in config["medidas"].items():
        menor, maior = m["limite_fisico"]
        valor = ler_numero(f"  {m['rotulo']} ({menor:g} a {maior:g}{m['unidade']}): ", menor, maior)
        if valor is None:
            return
        medidas[nome] = valor
    risco = calcular_risco(medidas, config)
    faixa = classificar_faixa(risco, config)
    print(f"\n  Índice de risco: {risco:.1f} -> {faixa}")
    print(f"  AVISO: {config['avisos'][faixa]}\n")


def acao_exportar(estado: dict, config: Config) -> None:
    """Exporta o histórico para relatorio.csv."""
    if not estado["historico"]:
        print("\n  Histórico vazio. Use a opção 1 primeiro.\n")
        return
    if exportar_csv(estado["historico"], config):
        print(f"\n  {len(estado['historico'])} leituras exportadas para {ARQ_RELATORIO.name}\n")


def acao_buscar_horario(estado: dict, config: Config) -> None:
    """Pergunta um horário e mostra a leitura do histórico mais próxima dele."""
    historico = estado["historico"]
    if not historico:
        print("\n  Histórico vazio. Use a opção 1 primeiro.\n")
        return
    primeira, ultima = formatar_hora(historico[0]["horario"]), formatar_hora(historico[-1]["horario"])
    print(f"\n  O histórico vai de {primeira} a {ultima}.")
    ano = datetime.fromisoformat(historico[-1]["horario"]).year
    alvo = ler_horario("  Horário (DD/MM HH:MM, Enter cancela): ", ano)
    if alvo is None:
        return
    leitura = buscar_por_horario(historico, alvo)
    distancia = abs(datetime.fromisoformat(leitura["horario"]) - alvo)
    print(f"\n  Leitura mais próxima: {formatar_hora(leitura['horario'])}"
          f" (diferença de {formatar_duracao(distancia)})")
    print(f"  Risco {leitura['risco']:.1f} · {leitura['faixa']}  [{formatar_medidas(leitura['medidas'], config)}]\n")


def acao_comparar_dias(estado: dict) -> None:
    """Mostra a comparação entre o último dia do histórico e o anterior."""
    if not estado["historico"]:
        print("\n  Histórico vazio. Use a opção 1 primeiro.\n")
        return
    imprimir_linhas(["Comparação entre dias (último dia x anterior)", *comparar_dias(estado["historico"])])


def acao_config(config: Config) -> None:
    """Mostra limites, pesos e faixas usados no índice."""
    print("\n  Como o risco é calculado:")
    for m in config["medidas"].values():
        print(f"  - {m['rotulo']}: {m['min']:g} a {m['max']:g}{m['unidade']} · peso {m['peso']}")
    lim = config["limites_faixa"]
    print(f"  Faixas: NORMAL < {lim['ATENÇÃO']:g} · ATENÇÃO {lim['ATENÇÃO']:g} a {lim['PERIGO']:g} · PERIGO >= {lim['PERIGO']:g}")
    canal = config["thingspeak"]["canal_id"] or "(não configurado)"
    print(f"  Canal ThingSpeak: {canal}\n")


# ======================================================================
# 10. Menu principal
# ======================================================================
def mostrar_menu(config: Config) -> None:
    """Imprime as opções do menu."""
    print(f"--- {config['nome']} · {config['local']} ---")
    print("1. Buscar leituras do sensor (ThingSpeak)")
    print("2. Relatório do histórico completo")
    print("3. Ranking das 5 piores leituras")
    print("4. Simular uma leitura")
    print("5. Exportar relatorio.csv")
    print("6. Ver limites e pesos do índice")
    print("7. Buscar uma leitura por horário")
    print("8. Comparar o último dia com o anterior")
    print("0. Sair")


def main() -> None:
    """Valida a configuração, carrega o histórico e roda o menu até o usuário sair."""
    try:
        validar_config(CONFIG)
    except ValueError as erro:
        print(f"Configuração inválida: {erro}")
        return

    estado = {"historico": carregar_historico(CONFIG)}
    acoes = {
        "1": lambda: acao_buscar(estado, CONFIG),
        "2": lambda: acao_relatorio_historico(estado, CONFIG),
        "3": lambda: acao_ranking(estado, CONFIG),
        "4": lambda: acao_simular(CONFIG),
        "5": lambda: acao_exportar(estado, CONFIG),
        "6": lambda: acao_config(CONFIG),
        "7": lambda: acao_buscar_horario(estado, CONFIG),
        "8": lambda: acao_comparar_dias(estado),
    }
    while True:
        mostrar_menu(CONFIG)
        opcao = ler_texto("Escolha: ")
        if opcao is None or opcao == "0":
            print("Até logo. Em emergência, ligue 199 (Defesa Civil).")
            break
        acao = acoes.get(opcao)
        if acao is None:
            print("\n  Opção inválida. Digite um número de 0 a 8.\n")
        else:
            acao()


if __name__ == "__main__":
    main()
