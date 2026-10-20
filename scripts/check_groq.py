"""Diagnostic script to inspect available Groq models for your API key."""

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from groq import Groq
from src.core.config import settings


def main():
    api_key = settings.GROQ_API_KEY.get_secret_value().strip()
    print("=" * 60)
    print("🔍 INSPECCIONANDO MODELOS DISPONIBLES EN GROQ...")
    print(f"🔑 API Key terminada en: ...{api_key[-8:] if len(api_key) > 8 else 'N/A'}")
    print("=" * 60)

    try:
        client = Groq(api_key=api_key)
        models = client.models.list()
        active_models = [m.id for m in models.data]
        print("\n✅ Conexión exitosa con Groq. Modelos disponibles para tu cuenta:")
        for m in sorted(active_models):
            print(f" - {m}")

        # Filter out audio, guards, and specialized models
        chat_candidates = [
            m for m in active_models
            if "whisper" not in m
            and "guard" not in m
            and "orpheus" not in m
            and "allam" not in m
        ]
        # Prioritize gpt-oss models
        chat_candidates.sort(key=lambda x: (not x.startswith("openai/gpt-oss-20b"), not x.startswith("openai/gpt-oss-120b")))

        for test_model in chat_candidates:
            print(f"\n🧪 Probando inferencia con: {test_model}...")
            # Try 1: with response_format
            try:
                res = client.chat.completions.create(
                    model=test_model,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant. Output valid JSON only with {\"status\": \"ok\", \"model\": \"" + test_model + "\"}"},
                        {"role": "user", "content": "Return the JSON now."},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=500,
                    temperature=0.1,
                )
                print(f"🎉 Éxito con JSON mode ({test_model}):\n{res.choices[0].message.content.strip()}")
                print(f"\n👉 MODELO RECOMENDADO: {test_model}")
                break
            except Exception as e_json:
                print(f"   ⚠️ Falló con response_format en {test_model}: {e_json}")
                # Try 2: without response_format
                try:
                    res = client.chat.completions.create(
                        model=test_model,
                        messages=[
                            {"role": "system", "content": "Output valid JSON only with {\"status\": \"ok\", \"model\": \"" + test_model + "\"}"},
                            {"role": "user", "content": "Return the JSON now."},
                        ],
                        max_tokens=500,
                        temperature=0.1,
                    )
                    print(f"🎉 Éxito en modo texto puro ({test_model}):\n{res.choices[0].message.content.strip()}")
                    print(f"\n👉 MODELO RECOMENDADO: {test_model}")
                    break
                except Exception as e_plain:
                    print(f"   ❌ Falló también en modo texto puro: {e_plain}")

    except Exception as e:
        print(f"\n❌ Error al consultar modelos de Groq: {e}")


if __name__ == "__main__":
    main()
