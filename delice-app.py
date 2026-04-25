import streamlit as st
import random
import json
import urllib.parse
import time
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
def supprimer_recette(recette_id: str):
    supabase.table("recettes").delete().eq("id", recette_id).execute()
    st.success("Recette supprimée avec succès !")
    st.rerun()

def inventer_recette_ia(theme: str, options: List[str]) -> bool:
    options_str = ", ".join(options) if options else "Aucune restriction"
    res_existantes = supabase.table("recettes").select("nom").ilike("theme", theme).execute()
    noms_existants = [r["nom"] for r in res_existantes.data] if res_existantes.data else []
    exclusion_str = f"NE PROPOSE SURTOUT PAS ces recettes : {', '.join(noms_existants)}." if noms_existants else ""
    
    prompt = f"""
    Tu es un chef cuisinier expert. Invente UNE NOUVELLE recette originale du thème '{theme}'.
    {exclusion_str}
    Contraintes diététiques : {options_str}.
    Quantités pour EXACTEMENT 1 PERSONNE. 
    RÈGLE ABSOLUE : La clé "quantite" DOIT ÊTRE UN NOMBRE (ex: 2 ou 0.5), JAMAIS DE TEXTE.
    Format attendu JSON :
    {{
        "nom": "Nom", "instructions": "Les étapes.",
        "ingredients": [{{"nom": "Ingrédient", "rayon": "Rayon", "quantite": 1.5, "unite": "pièce(s)"}}]
    }}
    """
    
    max_tentatives = 3
    for tentative in range(max_tentatives):
        with st.spinner(f"🧠 L'IA cherche une nouvelle idée (Tentative {tentative + 1}/{max_tentatives})..."):
            try:
                config = genai.GenerationConfig(response_mime_type="application/json", temperature=0.7)
                response = model.generate_content(prompt, generation_config=config)
                recette_ia = json.loads(response.text)
                
                res_recette = supabase.table("recettes").insert({
                    "nom": recette_ia["nom"], "theme": theme, "instructions": recette_ia["instructions"]
                }).execute()
                recette_id = res_recette.data[0]["id"]
                
                for ing in recette_ia["ingredients"]:
                    nom_ing = str(ing["nom"]).strip().capitalize()
                    qte_ing = float(ing["quantite"])
                    rayon_ing = str(ing["rayon"]).strip()
                    unite_ing = str(ing["unite"]).strip()
                    
                    supabase.table("ingredients").upsert({"nom": nom_ing, "rayon": rayon_ing}, on_conflict="nom").execute()
                    res_ing = supabase.table("ingredients").select("id").eq("nom", nom_ing).execute()
                    supabase.table("recette_ingredients").insert({
                        "recette_id": recette_id, "ingredient_id": res_ing.data[0]["id"], 
                        "quantite": qte_ing, "unite": unite_ing
                    }).execute()
                    
                st.success(f"✨ L'IA a créé une nouveauté : {recette_ia['nom']} !")
                return True
                
            except Exception as e:
                erreur_str = str(e)
                if "429" in erreur_str or "quota" in erreur_str.lower():
                    if tentative < max_tentatives - 1:
                        st.warning("Google freine la cadence. Pause de 20 secondes de sécurité...")
                        time.sleep(20) 
                        continue 
                    else:
                        st.error("L'IA est trop sollicitée pour le moment. Laissez-lui 1 ou 2 minutes de repos ! ☕")
                        return False
                else:
                    st.error(f"Erreur technique : {erreur_str}")
                    return False

def generer_menu(theme: str, nb_repas: int, options: List[str]) -> None:
    res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").ilike("theme", theme).execute()
    recettes_dispo = res.data
    
    if len(recettes_dispo) < nb_repas:
        succes = inventer_recette_ia(theme, options)
        if succes:
            generer_menu(theme, nb_repas, options)
            return
        else:
            st.warning("Affichage des recettes déjà disponibles pour ce thème.")
            
    if recettes_dispo:
        st.session_state["theme_actuel"] = theme
        st.session_state["menu_actuel"] = random.sample(recettes_dispo, min(len(recettes_dispo), nb_repas))
        save_menu_supabase(st.session_state["menu_actuel"])
        st.rerun()
    else:
        st.error(f"Génération impossible : Aucune recette '{theme}' en base.")

def remplacer_une_recette(index: int, options: List[str]):
    theme = st.session_state["theme_actuel"]
    res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").ilike("theme", theme).execute()
    ids_actuels = [r["id"] for r in st.session_state["menu_actuel"]]
    potentielles = [r for r in res.data if r["id"] not in ids_actuels]
    
    if potentielles:
        st.session_state["menu_actuel"][index] = random.choice(potentielles)
        save_menu_supabase(st.session_state["menu_actuel"])
        st.rerun()
    else:
        if inventer_recette_ia(theme, options):
            remplacer_une_recette(index, options)

def calculer_courses(menu: List[Dict[str, Any]], nb_personnes: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    res_stock = supabase.table("frigo").select("quantite, ingredients(nom)").execute()
    stock_frigo = {item["ingredients"]["nom"].capitalize(): float(item["quantite"]) for item in res_stock.data} if res_stock.data else {}
    liste_courses: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for recette in menu:
        for lien in recette.get("recette_ingredients", []):
            nom_ing = lien.get("ingredients", {}).get("nom", "Inconnu").capitalize()
            rayon = lien.get("ingredients", {}).get("rayon", "Autre")
            besoin = float(lien.get("quantite", 0)) * nb_personnes
            qte_finale = besoin - stock_frigo.get(nom_ing, 0.0)
            if qte_finale > 0:
                if rayon not in liste_courses: liste_courses[rayon] = {}
                if nom_ing not in liste_courses[rayon]: liste_courses[rayon][nom_ing] = {"quantite": 0.0, "unite": lien.get("unite", "")}
                liste_courses[rayon][nom_ing]["quantite"] += qte_finale
    return liste_courses

def afficher_details_recette(recette):
    """Fonction utilitaire pour afficher proprement une recette (Ingrédients + Préparation)"""
    st.markdown("**🛒 Ingrédients (pour 1 personne) :**")
    for lien in recette.get("recette_ingredients", []):
        nom_ing = lien.get("ingredients", {}).get("nom", "Inconnu").capitalize()
        qte = lien.get("quantite", 0)
        qte_propre = int(qte) if isinstance(qte, float) and qte.is_integer() else qte
        unite = lien.get("unite", "")
        st.markdown(f"- {qte_propre} {unite} {nom_ing}")
    
    st.markdown("**👨‍🍳 Préparation :**")
    st.write(recette.get('instructions', "Aucune instruction."))

# ==========================================
# 4. INTERFACE UTILISATEUR (GUI)
# ==========================================
with st.sidebar:
    st.header("⚙️ Paramètres")
    nb_p = st.number_input("👥 Personnes", min_value=1, value=2)
    nb_r = st.number_input("🍽️ Repas", min_value=1, value=3)
    opt = st.multiselect("🥗 Options", ["Léger", "Hyperprotéiné", "Végétarien", "Économique"])
    st.divider()
    res_t = supabase.table("recettes").select("theme").execute()
    themes_bruts = list(set([r["theme"] for r in res_t.data])) if res_t.data else []
    themes_fixes = ["Asiatique", "Orientale", "Italienne", "Classique"]
    all_themes = sorted(list(set(themes_fixes + themes_bruts)))
    
    chx = st.selectbox("Thème", all_themes + ["+ Créer..."])
    th_f = st.text_input("Nom nouveau thème") if chx == "+ Créer..." else chx
    
    if st.button("🚀 Générer la Semaine", use_container_width=True, type="primary"):
        generer_menu(th_f, nb_r, opt)

st.title("🍲 DÉLICE-APP")
tab_m, tab_f, tab_l = st.tabs(["🍽️ Menu & Courses", "🧊 Mon Frigo", "📚 Livre de Recettes"])

with tab_m:
    if st.session_state["menu_actuel"]:
        c_m, c_c = st.columns([1.2, 1])
        with c_m:
            st.subheader(f"Menu {st.session_state['theme_actuel']}")
            if st.button("➕ Ajouter une nouvelle recette via l'IA"):
                if inventer_recette_ia(st.session_state["theme_actuel"], opt):
                    st.rerun()
            
            for i, rec in enumerate(st.session_state["menu_actuel"]):
                with st.expander(f"**{rec['nom']}**"):
                    # On utilise la nouvelle fonction d'affichage
                    afficher_details_recette(rec)
                    
                    st.divider()
                    if st.button(f"🔄 Remplacer", key=f"swap_{i}"):
                        remplacer_une_recette(i, opt)
        with c_c:
            st.subheader("🛒 Courses")
            crs = calculer_courses(st.session_state["menu_actuel"], nb_p)
            for r, ings in crs.items():
                st.write(f"**{r}**")
                for n, d in ings.items():
                    q = int(d['quantite']) if d['quantite'].is_integer() else round(d['quantite'], 2)
                    # La correction anti-doublon est bien là :
                    st.checkbox(f"{n} : {q} {d['unite']}", key=f"ch_{r}_{n}")
            
            st.divider()
            txt_exp = "🛒 *Liste de Courses*\n\n" + "".join([f"📍 *{r}*\n" + "".join([f"☐ {n} : {d['quantite']} {d['unite']}\n" for n, d in ings.items()]) + "\n" for r, ings in crs.items()])
            st.markdown(f'<a href="https://wa.me/?text={urllib.parse.quote(txt_exp)}" target="_blank" style="background:#25D366; color:white; padding:10px; border-radius:5px; text-decoration:none; display:block; text-align:center;">📱 Envoyer sur WhatsApp</a>', unsafe_allow_html=True)
    else: st.info("Utilisez le menu latéral pour commencer.")

with tab_f:
    st.subheader("🧊 Stocks")
    with st.expander("➕ Ajouter"):
        ri = supabase.table("ingredients").select("id, nom").execute()
        di = {i["nom"].capitalize(): i["id"] for i in ri.data} if ri.data else {}
        ca1, ca2, ca3 = st.columns([2, 1, 1])
        n_s = ca1.selectbox("Ingrédient", [""] + sorted(list(di.keys())))
        q_s = ca2.number_input("Quantité", min_value=0.0, value=1.0)
        if ca3.button("OK") and n_s:
            supabase.table("frigo").upsert({"ingredient_id": di[n_s], "quantite": q_s}, on_conflict="ingredient_id").execute()
            st.rerun()
    res_f = supabase.table("frigo").select("quantite, ingredient_id, ingredients(nom)").execute()
    for it in res_f.data:
        cn, cq, cb = st.columns([3, 1, 1])
        cn.write(it["ingredients"]["nom"])
        cq.write(str(it["quantite"]))
        if cb.button("🗑️", key=f"dl_{it['ingredient_id']}"):
            supabase.table("frigo").delete().eq("ingredient_id", it['ingredient_id']).execute()
            st.rerun()

with tab_l:
    st.subheader("📚 Livre de Recettes")
    # On a ajouté la demande des ingrédients dans la requête Supabase !
    all_r = supabase.table("recettes").select("id, nom, theme, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").execute()
    if all_r.data:
        themes_presents = sorted(list(set([r['theme'] for r in all_r.data])))
        for t in themes_presents:
            st.markdown(f"### 📍 {t}")
            recettes_du_theme = [r for r in all_r.data if r['theme'] == t]
            for r in recettes_du_theme:
                c1, c2 = st.columns([5, 1])
                with c1:
                    with st.expander(f"📖 {r['nom']}"):
                        # On réutilise la même fonction pour le livre
                        afficher_details_recette(r)
                with c2:
                    if st.button("🗑️", key=f"del_rec_{r['id']}"):
                        supprimer_recette(r['id'])
    else: st.info("Aucune recette en mémoire.")