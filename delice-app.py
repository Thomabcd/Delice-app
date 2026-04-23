import streamlit as st
import random
from typing import List, Dict, Any
from supabase import create_client, Client
import google.generativeai as genai

# ==========================================
# 1. CONFIGURATION
# ==========================================
st.set_page_config(page_title="Délice-App", page_icon="🍲", layout="wide")

@st.cache_resource
def init_connection() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_connection()
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-2.5-flash')

# ==========================================
# 2. PERSISTANCE CLOUD (SUPABASE)
# ==========================================
def save_menu_supabase(menu_data: List[Dict[str, Any]]) -> None:
    anciens = supabase.table("menu_en_cours").select("id").execute()
    for row in anciens.data:
        supabase.table("menu_en_cours").delete().eq("id", row["id"]).execute()
    inserts = [{"recette_id": r["id"]} for r in menu_data]
    if inserts:
        supabase.table("menu_en_cours").insert(inserts).execute()

def load_menu_supabase() -> List[Dict[str, Any]]:
    res = supabase.table("menu_en_cours").select(
        "recettes(id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon)))"
    ).execute()
    return [item["recettes"] for item in res.data if item.get("recettes")]

if "menu_actuel" not in st.session_state:
    st.session_state["menu_actuel"] = load_menu_supabase()
if "theme_actuel" not in st.session_state:
    st.session_state["theme_actuel"] = "Classique"

# ==========================================
# 3. LOGIQUE MÉTIER & IA
# ==========================================
def inventer_recette_ia(theme: str, options: List[str]) -> bool:
    options_str = ", ".join(options) if options else "Aucune restriction"
    prompt = f"""
    Tu es un chef cuisinier expert. Invente UNE recette du thème '{theme}'.
    Contraintes diététiques : {options_str}.
    ATTENTION : Les quantités des ingrédients doivent être calculées pour EXACTEMENT 1 PERSONNE.
    Réponds STRICTEMENT avec un objet JSON valide.
    Format attendu :
    {{
        "nom": "Nom de la recette", "instructions": "Les étapes brèves.",
        "ingredients": [
            {{"nom": "Nom ingrédient", "rayon": "Légumes/Viandes/Épicerie/Frais", "quantite": 2.5, "unite": "pièce(s) ou g ou ml"}}
        ]
    }}
    """
    with st.spinner(f"🧠 L'IA invente une recette {theme}..."):
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
                supabase.table("recette_ingredients").insert({
                    "recette_id": recette_id, "ingredient_id": res_ing.data[0]["id"], 
                    "quantite": ing["quantite"], "unite": ing["unite"]
                }).execute()
            st.success(f"✨ Nouvelle recette mémorisée : {recette_ia['nom']} !")
            return True
        except Exception:
            st.error("L'IA a eu un petit problème. Réessayez !")
            return False

def generer_menu(theme: str, nb_repas: int, options: List[str]) -> None:
    st.session_state["theme_actuel"] = theme
    res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").eq("theme", theme).execute()
    recettes_dispo = res.data
    
    if len(recettes_dispo) < nb_repas:
        if inventer_recette_ia(theme, options):
            generer_menu(theme, nb_repas, options)
        return

    menu_genere = random.sample(recettes_dispo, min(len(recettes_dispo), nb_repas))
    st.session_state["menu_actuel"] = menu_genere
    save_menu_supabase(menu_genere)
    st.rerun()

def get_stock_frigo() -> Dict[str, float]:
    """Récupère les stocks actuels du frigo."""
    res = supabase.table("frigo").select("quantite, ingredients(nom)").execute()
    return {item["ingredients"]["nom"].capitalize(): float(item["quantite"]) for item in res.data} if res.data else {}

def calculer_courses(menu: List[Dict[str, Any]], nb_personnes: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    stock_frigo = get_stock_frigo()
    liste_courses: Dict[str, Dict[str, Dict[str, Any]]] = {}
    
    for recette in menu:
        for lien in recette.get("recette_ingredients", []):
            nom_ing = lien.get("ingredients", {}).get("nom", "Inconnu").capitalize()
            rayon = lien.get("ingredients", {}).get("rayon", "Autre")
            besoin_total = float(lien.get("quantite", 0)) * nb_personnes
            unite = lien.get("unite", "")

            # Déduction intelligente du stock
            stock_dispo = stock_frigo.get(nom_ing, 0.0)
            quantite_a_acheter = besoin_total - stock_dispo

            # On ajoute à la liste SEULEMENT si on en a besoin
            if quantite_a_acheter > 0:
                if rayon not in liste_courses: liste_courses[rayon] = {}
                if nom_ing not in liste_courses[rayon]: liste_courses[rayon][nom_ing] = {"quantite": 0.0, "unite": unite}
                liste_courses[rayon][nom_ing]["quantite"] += quantite_a_acheter
    return liste_courses

# ==========================================
# 4. INTERFACE UTILISATEUR (GUI)
# ==========================================
with st.sidebar:
    st.header("⚙️ Paramètres")
    nb_personnes = st.number_input("👥 Nombre de personnes", min_value=1, max_value=20, value=2)
    nb_repas = st.number_input("🍽️ Nombre de repas", min_value=1, max_value=14, value=3)
    options_diet = st.multiselect("🥗 Options diététiques", ["Léger", "Hyperprotéiné", "Végétarien", "Sans Gluten", "Économique"])
    
    st.divider()
    st.header("🎨 Thème")
    res_themes = supabase.table("recettes").select("theme").execute()
    themes_existants = list(set([r["theme"] for r in res_themes.data])) if res_themes.data else ["Classique"]
    choix_theme = st.selectbox("Choisissez un thème", themes_existants + ["+ Créer un nouveau thème..."])
    theme_final = st.text_input("Nom du nouveau thème") if choix_theme == "+ Créer un nouveau thème..." else choix_theme

    if st.button("🚀 Générer la Semaine", use_container_width=True, type="primary"):
        if theme_final: generer_menu(theme_final, nb_repas, options_diet)
        else: st.warning("Veuillez définir un thème.")

st.title("🍲 DÉLICE-APP")

# Création des onglets
onglet_menu, onglet_frigo = st.tabs(["🍽️ Menu & Courses", "🧊 Mon Frigo (Stocks)"])

with onglet_menu:
    if st.session_state["menu_actuel"]:
        col_menu, col_courses = st.columns([1.2, 1])

        with col_menu:
            st.subheader(f"Menu {st.session_state['theme_actuel']}")
            if st.button("✨ Forcer l'IA à inventer une nouvelle recette"):
                if inventer_recette_ia(st.session_state["theme_actuel"], options_diet):
                    generer_menu(st.session_state["theme_actuel"], nb_repas, options_diet)
                
            for recette in st.session_state["menu_actuel"]:
                with st.expander(f"**{recette['nom']}**"):
                    st.write(f"_{recette['instructions']}_")

        with col_courses:
            st.subheader("🛒 Liste de Courses Optimisée")
            st.caption(f"Calculé pour {nb_personnes} pers. (Stocks déduits)")
            courses = calculer_courses(st.session_state["menu_actuel"], nb_personnes)
            
            if not courses:
                st.success("🎉 Vous avez déjà tout ce qu'il faut dans votre frigo !")
            else:
                for rayon, ingredients in courses.items():
                    st.markdown(f"**📍 {rayon}**")
                    for nom, data in ingredients.items():
                        qte = int(data['quantite']) if data['quantite'].is_integer() else round(data['quantite'], 2)
                        st.checkbox(f"{nom} : {qte} {data['unite']}")
    else:
        st.info("👈 Générez vos repas depuis le menu latéral !")

with onglet_frigo:
    st.subheader("Gérez vos réserves")
    st.write("Dites à l'application ce que vous possédez déjà. Elle déduira ces quantités de vos futures listes de courses !")
    
    # Récupérer tous les ingrédients existants pour le menu déroulant
    res_tous_ing = supabase.table("ingredients").select("id, nom").execute()
    liste_noms_ing = {ing["nom"].capitalize(): ing["id"] for ing in res_tous_ing.data} if res_tous_ing.data else {}
    
    col_ajout1, col_ajout2, col_ajout3 = st.columns([2, 1, 1])
    with col_ajout1:
        ing_a_ajouter = st.selectbox("Sélectionner un ingrédient", [""] + list(liste_noms_ing.keys()))
    with col_ajout2:
        qte_a_ajouter = st.number_input("Quantité en stock", min_value=0.0, value=1.0, step=0.5)
    with col_ajout3:
        if st.button("➕ Ajouter au Frigo") and ing_a_ajouter:
            id_ing = liste_noms_ing[ing_a_ajouter]
            # Upsert : Met à jour si existe, sinon insère
            supabase.table("frigo").upsert({"ingredient_id": id_ing, "quantite": qte_a_ajouter}).execute()
            st.rerun()

    st.divider()
    st.write("**Contenu actuel de votre Frigo :**")
    stocks = get_stock_frigo()
    if stocks:
        for nom, qte in stocks.items():
            st.write(f"- **{nom}** : {qte}")
    else:
        st.info("Votre frigo virtuel est vide.")