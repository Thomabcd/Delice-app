import streamlit as st
import random
import json
import urllib.parse
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
    """
    with st.spinner(f"🧠 L'IA invente une recette..."):
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
            return True
        except: return False

def generer_menu(theme: str, nb_repas: int, options: List[str]) -> None:
    st.session_state["theme_actuel"] = theme
    res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").eq("theme", theme).execute()
    recettes_dispo = res.data
    if len(recettes_dispo) < nb_repas:
        if inventer_recette_ia(theme, options): generer_menu(theme, nb_repas, options)
        return
    menu_genere = random.sample(recettes_dispo, min(len(recettes_dispo), nb_repas))
    st.session_state["menu_actuel"] = menu_genere
    save_menu_supabase(menu_genere)
    st.rerun()

def get_stock_frigo_complet() -> List[Dict[str, Any]]:
    res = supabase.table("frigo").select("quantite, ingredient_id, ingredients(nom)").execute()
    return res.data if res.data else []

def calculer_courses(menu: List[Dict[str, Any]], nb_personnes: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    res_stock = get_stock_frigo_complet()
    stock_frigo = {item["ingredients"]["nom"].capitalize(): float(item["quantite"]) for item in res_stock}
    
    liste_courses: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for recette in menu:
        for lien in recette.get("recette_ingredients", []):
            nom_ing = lien.get("ingredients", {}).get("nom", "Inconnu").capitalize()
            rayon = lien.get("ingredients", {}).get("rayon", "Autre")
            besoin_total = float(lien.get("quantite", 0)) * nb_personnes
            unite = lien.get("unite", "")

            quantite_a_acheter = besoin_total - stock_frigo.get(nom_ing, 0.0)

            if quantite_a_acheter > 0:
                if rayon not in liste_courses: liste_courses[rayon] = {}
                if nom_ing not in liste_courses[rayon]: liste_courses[rayon][nom_ing] = {"quantite": 0.0, "unite": unite}
                liste_courses[rayon][nom_ing]["quantite"] += quantite_a_acheter
    return liste_courses

def estimer_budget_ia(courses: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    if not courses: return "0€ (Rien à acheter !)"
    liste_texte = ""
    for r, ings in courses.items():
        for n, d in ings.items():
            liste_texte += f"- {d['quantite']} {d['unite']} de {n}\n"
    prompt = f"Tu es un expert en budget de courses en France. Voici une liste : {liste_texte}. Donne-moi UNIQUEMENT une fourchette de prix estimée en euros (ex: 45€ - 55€). Ne justifie pas."
    try: return model.generate_content(prompt).text.strip()
    except: return "Estimation indisponible"

def formatter_courses_texte(courses: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    """Transforme le dictionnaire de courses en un texte propre pour l'export."""
    if not courses: return "Rien à acheter cette semaine ! 🎉"
    texte = "🛒 *Liste de Courses Délice-App* 🍲\n\n"
    for rayon, ingredients in courses.items():
        texte += f"📍 *{rayon}*\n"
        for nom, data in ingredients.items():
            qte = int(data['quantite']) if data['quantite'].is_integer() else round(data['quantite'], 2)
            texte += f"☐ {nom} : {qte} {data['unite']}\n"
        texte += "\n"
    return texte

# ==========================================
# 4. INTERFACE UTILISATEUR (GUI)
# ==========================================
with st.sidebar:
    st.header("⚙️ Paramètres")
    nb_personnes = st.number_input("👥 Personnes", min_value=1, value=2)
    nb_repas = st.number_input("🍽️ Repas", min_value=1, value=3)
    options_diet = st.multiselect("🥗 Options", ["Léger", "Hyperprotéiné", "Végétarien", "Économique"])
    
    st.divider()
    res_themes = supabase.table("recettes").select("theme").execute()
    themes_existants = list(set([r["theme"] for r in res_themes.data])) if res_themes.data else ["Classique"]
    choix_theme = st.selectbox("Thème", themes_existants + ["+ Créer..."])
    theme_final = st.text_input("Nouveau thème") if choix_theme == "+ Créer..." else choix_theme

    if st.button("🚀 Générer", use_container_width=True, type="primary"):
        generer_menu(theme_final, nb_repas, options_diet)

st.title("🍲 DÉLICE-APP")
onglet_menu, onglet_frigo = st.tabs(["🍽️ Menu & Courses", "🧊 Gestion du Frigo"])

with onglet_menu:
    if st.session_state["menu_actuel"]:
        col_m, col_c = st.columns([1.2, 1])
        with col_m:
            st.subheader(f"Menu {st.session_state['theme_actuel']}")
            for recette in st.session_state["menu_actuel"]:
                with st.expander(f"**{recette['nom']}**"):
                    st.write(recette['instructions'])
        with col_c:
            st.subheader("🛒 Courses")
            courses = calculer_courses(st.session_state["menu_actuel"], nb_personnes)
            
            if not courses:
                st.success("🎉 Vous avez déjà tout ce qu'il faut dans votre frigo !")
            else:
                # Affichage interactif
                for rayon, ingredients in courses.items():
                    st.write(f"**{rayon}**")
                    for nom, data in ingredients.items():
                        qte = int(data['quantite']) if data['quantite'].is_integer() else round(data['quantite'], 2)
                        st.checkbox(f"{nom} : {qte} {data['unite']}", key=f"shop_{nom}")
                
                st.divider()
                
                # --- NOUVEAU : ZONE D'EXPORT ---
                st.subheader("📤 Exporter")
                texte_export = formatter_courses_texte(courses)
                
                # 1. Zone de texte rapide à copier
                st.text_area("Copiez ce texte pour vos notes :", value=texte_export, height=150)
                
                # 2. Bouton WhatsApp natif
                texte_encode = urllib.parse.quote(texte_export)
                lien_wa = f"https://wa.me/?text={texte_encode}"
                st.markdown(f'<a href="{lien_wa}" target="_blank" style="display: inline-block; padding: 0.5em 1em; color: white; background-color: #25D366; border-radius: 5px; text-decoration: none; font-weight: bold;">📱 Partager sur WhatsApp</a>', unsafe_allow_html=True)
                
                st.divider()
                # Estimation budget
                if st.button("💰 Estimer le budget", use_container_width=True):
                    with st.spinner("Calcul en cours..."):
                        estimation = estimer_budget_ia(courses)
                        st.info(f"**Budget estimé : {estimation}**")
    else:
        st.info("Utilisez le menu latéral pour commencer !")

with onglet_frigo:
    st.subheader("🧊 Inventaire de vos réserves")
    with st.expander("➕ Ajouter ou modifier un article", expanded=True):
        res_tous_ing = supabase.table("ingredients").select("id, nom").execute()
        dict_ing = {i["nom"].capitalize(): i["id"] for i in res_tous_ing.data} if res_tous_ing.data else {}
        
        c1, c2, c3 = st.columns([2, 1, 1])
        nom_sel = c1.selectbox("Ingrédient", [""] + sorted(list(dict_ing.keys())))
        qte_sel = c2.number_input("Quantité", min_value=0.0, value=1.0)
        if c3.button("Enregistrer", use_container_width=True) and nom_sel:
            supabase.table("frigo").upsert({"ingredient_id": dict_ing[nom_sel], "quantite": qte_sel}, on_conflict="ingredient_id").execute()
            st.success(f"{nom_sel} mis à jour !")
            st.rerun()

    st.divider()
    stocks = get_stock_frigo_complet()
    if stocks:
        for item in stocks:
            nom_item = item["ingredients"]["nom"].capitalize()
            id_ing = item["ingredient_id"]
            qte_item = item["quantite"]
            col_n, col_q, col_b = st.columns([3, 1, 1])
            col_n.write(f"**{nom_item}**")
            col_q.write(f"{qte_item}")
            if col_b.button("🗑️", key=f"del_{id_ing}"):
                supabase.table("frigo").delete().eq("ingredient_id", id_ing).execute()
                st.rerun()
    else:
        st.info("Votre frigo est vide.")