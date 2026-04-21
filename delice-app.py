import streamlit as st
import json
import os
import random
from typing import List, Dict, Any
from supabase import create_client, Client
import google.generativeai as genai

# ==========================================
# 1. CONFIGURATION
# ==========================================
st.set_page_config(page_title="Délice-App", page_icon="🍲", layout="wide")
LOCAL_SAVE_FILE = "data.json"

@st.cache_resource
def init_connection() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_connection()
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-2.5-flash')

# ==========================================
# 2. PERSISTANCE LOCALE
# ==========================================
def save_menu_local(menu_data: List[Dict[str, Any]]) -> None:
    with open(LOCAL_SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(menu_data, f, ensure_ascii=False, indent=4)

def load_menu_local() -> List[Dict[str, Any]]:
    if os.path.exists(LOCAL_SAVE_FILE):
        with open(LOCAL_SAVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

if "menu_actuel" not in st.session_state:
    st.session_state["menu_actuel"] = load_menu_local()
if "theme_actuel" not in st.session_state:
    st.session_state["theme_actuel"] = "Classique"

# ==========================================
# 3. LOGIQUE MÉTIER & IA
# ==========================================
def inventer_recette_ia(theme: str, options: List[str]) -> None:
    """L'IA invente une recette basée sur le thème ET les options."""
    options_str = ", ".join(options) if options else "Aucune restriction"
    
    prompt = f"""
    Tu es un chef cuisinier expert. Invente UNE recette du thème '{theme}'.
    Contraintes diététiques à respecter IMPÉRATIVEMENT : {options_str}.
    ATTENTION : Les quantités des ingrédients doivent être calculées pour EXACTEMENT 1 PERSONNE.
    Réponds STRICTEMENT avec un objet JSON valide, sans aucun texte autour.
    Format attendu :
    {{
        "nom": "Nom de la recette",
        "instructions": "Les étapes brèves.",
        "ingredients": [
            {{"nom": "Nom ingrédient", "rayon": "Légumes/Viandes/Épicerie/Frais", "quantite": 2.5, "unite": "pièce(s) ou g ou ml"}}
        ]
    }}
    """
    
    with st.spinner(f"🧠 L'IA invente une recette {theme} (Options: {options_str})..."):
        try:
            response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            recette_ia = json.loads(response.text)
            
            res_recette = supabase.table("recettes").insert({
                "nom": recette_ia["nom"], "theme": theme, "instructions": recette_ia["instructions"]
            }).execute()
            recette_id = res_recette.data[0]["id"]
            
            for ing in recette_ia["ingredients"]:
                supabase.table("ingredients").upsert({"nom": ing["nom"], "rayon": ing["rayon"]}, on_conflict="nom").execute()
                res_ing = supabase.table("ingredients").select("id").eq("nom", ing["nom"]).execute()
                ing_id = res_ing.data[0]["id"]
                supabase.table("recette_ingredients").insert({
                    "recette_id": recette_id, "ingredient_id": ing_id, 
                    "quantite": ing["quantite"], "unite": ing["unite"]
                }).execute()
                
            st.success(f"✨ Nouvelle recette mémorisée : {recette_ia['nom']} !")
            return True
        except Exception as e:
            st.error("L'IA a eu un petit problème de créativité. Réessayez !")
            return False

def generer_menu(theme: str, nb_repas: int, options: List[str]) -> None:
    """Récupère ou crée des recettes pour remplir le nombre de repas demandé."""
    st.session_state["theme_actuel"] = theme
    response = supabase.table("recettes").select(
        "nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))"
    ).eq("theme", theme).execute()
    
    recettes_dispo = response.data
    
    # Si on n'a pas assez de recettes dans la base pour le nombre de repas demandé
    if len(recettes_dispo) < nb_repas:
        if inventer_recette_ia(theme, options):
            generer_menu(theme, nb_repas, options) # On relance après création
        return

    menu_genere = random.sample(recettes_dispo, min(len(recettes_dispo), nb_repas))
    st.session_state["menu_actuel"] = menu_genere
    save_menu_local(menu_genere)
    st.rerun()

def calculer_courses(menu: List[Dict[str, Any]], nb_personnes: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Additionne les ingrédients et multiplie par le nombre de personnes."""
    liste_courses: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for recette in menu:
        for lien in recette.get("recette_ingredients", []):
            ing_data = lien.get("ingredients", {})
            nom_ing = ing_data.get("nom", "Inconnu").capitalize()
            rayon = ing_data.get("rayon", "Autre")
            
            # Multiplication par le nombre de personnes
            quantite = float(lien.get("quantite", 0)) * nb_personnes
            unite = lien.get("unite", "")

            if rayon not in liste_courses: liste_courses[rayon] = {}
            if nom_ing not in liste_courses[rayon]: liste_courses[rayon][nom_ing] = {"quantite": 0.0, "unite": unite}
            liste_courses[rayon][nom_ing]["quantite"] += quantite
    return liste_courses

# ==========================================
# 4. INTERFACE UTILISATEUR (GUI)
# ==========================================
# --- BARRE LATÉRALE (PARAMÈTRES) ---
with st.sidebar:
    st.header("⚙️ Paramètres")
    nb_personnes = st.number_input("👥 Nombre de personnes", min_value=1, max_value=20, value=2)
    nb_repas = st.number_input("🍽️ Nombre de repas", min_value=1, max_value=14, value=3)
    options_diet = st.multiselect("🥗 Options diététiques", ["Léger", "Hyperprotéiné", "Végétarien", "Sans Gluten", "Économique"])
    
    st.divider()
    
    st.header("🎨 Thème")
    # On récupère les thèmes existants de la base
    res_themes = supabase.table("recettes").select("theme").execute()
    themes_existants = list(set([r["theme"] for r in res_themes.data])) if res_themes.data else ["Classique"]
    
    choix_theme = st.selectbox("Choisissez un thème existant", themes_existants + ["+ Créer un nouveau thème..."])
    
    if choix_theme == "+ Créer un nouveau thème...":
        theme_final = st.text_input("Nom du nouveau thème (ex: Mexicain, Brunch...)")
    else:
        theme_final = choix_theme

    if st.button("🚀 Générer la Semaine", use_container_width=True, type="primary"):
        if theme_final:
            generer_menu(theme_final, nb_repas, options_diet)
        else:
            st.warning("Veuillez définir un thème.")

# --- ZONE PRINCIPALE ---
st.title("🍲 DÉLICE-APP")

if st.session_state["menu_actuel"]:
    col_menu, col_courses = st.columns([1.2, 1])

    with col_menu:
        st.subheader(f"🍽️ Menu {st.session_state['theme_actuel']}")
        
        # Bouton pour forcer l'IA à inventer une NOUVELLE recette dans ce thème
        if st.button("✨ L'IA invente une nouvelle recette pour ce thème"):
            inventer_recette_ia(st.session_state["theme_actuel"], options_diet)
            generer_menu(st.session_state["theme_actuel"], nb_repas, options_diet)
            
        for recette in st.session_state["menu_actuel"]:
            with st.expander(f"**{recette['nom']}**"):
                st.write(f"_{recette['instructions']}_")

    with col_courses:
        st.subheader("🛒 Liste de Courses")
        st.caption(f"Calculé pour {nb_personnes} personne(s)")
        courses = calculer_courses(st.session_state["menu_actuel"], nb_personnes)
        
        # Affichage avec cases à cocher
        for rayon, ingredients in courses.items():
            st.markdown(f"**📍 {rayon}**")
            for nom, data in ingredients.items():
                qte_propre = int(data['quantite']) if data['quantite'].is_integer() else round(data['quantite'], 2)
                # st.checkbox crée une case à cocher interactive
                st.checkbox(f"{nom} : {qte_propre} {data['unite']}")
else:
    st.info("👈 Utilisez la barre latérale pour configurer et générer vos repas !")