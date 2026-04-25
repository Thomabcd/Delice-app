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
    inserts = [{"recette_id": r["id"]} for r in menu_data if "id" in r]
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
    st.session_state["theme_actuel"] = "Sélection personnalisée"

# ==========================================
# 3. LOGIQUE MÉTIER & IA
# ==========================================
def ajouter_recette_manuelle(nom, theme, instructions, ingredients_bruts):
    try:
        res_recette = supabase.table("recettes").insert({
            "nom": nom, "theme": theme, "instructions": instructions
        }).execute()
        recette_id = res_recette.data[0]["id"]
        
        for ing in ingredients_bruts:
            nom_ing = str(ing["nom"]).strip().capitalize()
            supabase.table("ingredients").upsert({"nom": nom_ing, "rayon": ing["rayon"]}, on_conflict="nom").execute()
            res_ing = supabase.table("ingredients").select("id").eq("nom", nom_ing).execute()
            supabase.table("recette_ingredients").insert({
                "recette_id": recette_id, "ingredient_id": res_ing.data[0]["id"], 
                "quantite": float(ing["qte"]), "unite": ing["unite"]
            }).execute()
        return True
    except Exception as e:
        st.error(f"Erreur d'ajout : {e}")
        return False

def inventer_recette_ia(theme: str, options: List[str]) -> bool:
    options_str = ", ".join(options) if options else "Aucune restriction"
    res_existantes = supabase.table("recettes").select("nom").ilike("theme", theme).execute()
    noms_existants = [r["nom"] for r in res_existantes.data] if res_existantes.data else []
    exclusion_str = f"NE PROPOSE SURTOUT PAS ces recettes : {', '.join(noms_existants)}." if noms_existants else ""
    
    prompt = f"""
    Chef expert. Invente UNE NOUVELLE recette originale '{theme}'.
    {exclusion_str}
    Options: {options_str}. Quantités pour 1 PERSONNE. 
    JSON strict. "quantite" DOIT ETRE UN NOMBRE.
    Format attendu:
    {{
        "nom": "Nom", "instructions": "Étapes.",
        "ingredients": [{{"nom": "Ing", "rayon": "Rayon", "quantite": 1.5, "unite": "g"}}]
    }}
    """
    
    max_tentatives = 3
    for tentative in range(max_tentatives):
        with st.spinner(f"🧠 L'IA cherche une idée (Essai {tentative + 1}/{max_tentatives})..."):
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
                    supabase.table("ingredients").upsert({"nom": nom_ing, "rayon": str(ing["rayon"]).strip()}, on_conflict="nom").execute()
                    res_ing = supabase.table("ingredients").select("id").eq("nom", nom_ing).execute()
                    supabase.table("recette_ingredients").insert({
                        "recette_id": recette_id, "ingredient_id": res_ing.data[0]["id"], 
                        "quantite": float(ing["quantite"]), "unite": str(ing["unite"]).strip()
                    }).execute()
                    
                st.success(f"✨ L'IA a créé : {recette_ia['nom']} !")
                return True
                
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    if tentative < max_tentatives - 1:
                        time.sleep(20)
                        continue
                    else:
                        st.error("L'IA est trop sollicitée (Quota). Pause ! ☕")
                        return False
                else:
                    st.error(f"Erreur technique : {e}")
                    return False

def generer_menu(theme: str, nb_repas: int, options: List[str]) -> None:
    res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").ilike("theme", theme).execute()
    if len(res.data) < nb_repas:
        if inventer_recette_ia(theme, options):
            generer_menu(theme, nb_repas, options)
        return
        
    st.session_state["theme_actuel"] = theme
    st.session_state["menu_actuel"] = random.sample(res.data, min(len(res.data), nb_repas))
    save_menu_supabase(st.session_state["menu_actuel"])
    st.rerun()

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

def estimer_budget_ia(courses: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    if not courses: return "0€"
    txt = "".join([f"- {d['quantite']} {d['unite']} {n}\n" for r, i in courses.items() for n, d in i.items()])
    prompt = f"Expert budget courses France. Liste: {txt}. Donne juste une fourchette de prix (€)."
    try: return model.generate_content(prompt).text.strip()
    except: return "Indisponible"

def afficher_details_recette(recette):
    st.markdown("**🛒 Ingrédients (1 pers.) :**")
    for lien in recette.get("recette_ingredients", []):
        nom = lien.get("ingredients", {}).get("nom", "Inconnu").capitalize()
        qte = lien.get("quantite", 0)
        qte_p = int(qte) if isinstance(qte, float) and qte.is_integer() else round(qte, 2)
        st.markdown(f"- {qte_p} {lien.get('unite', '')} {nom}")
    st.markdown("**👨‍🍳 Préparation :**")
    st.write(recette.get('instructions', "Aucune étape."))

# ==========================================
# 4. INTERFACE UTILISATEUR (GUI)
# ==========================================
with st.sidebar:
    st.header("⚙️ Paramètres")
    nb_p = st.number_input("👥 Personnes", min_value=1, value=2)
    nb_r = st.number_input("🍽️ Repas", min_value=1, value=3)
    opt = st.multiselect("🥗 Options", ["Léger", "Hyperprotéiné", "Végétarien", "Économique"])
    
    if st.button("🗑️ Vider le menu actuel", use_container_width=True):
        st.session_state["menu_actuel"] = []
        save_menu_supabase([])
        st.session_state["theme_actuel"] = "Sélection personnalisée"
        st.rerun()

    st.divider()
    res_t = supabase.table("recettes").select("theme").execute()
    themes = sorted(list(set([r["theme"] for r in res_t.data]))) if res_t.data else ["Classique"]
    chx = st.selectbox("Générer par thème", themes + ["+ Créer..."])
    th_f = st.text_input("Nouveau thème") if chx == "+ Créer..." else chx
    
    if st.button("🚀 Générer Auto", use_container_width=True, type="primary"):
        generer_menu(th_f, nb_r, opt)

st.title("🍲 DÉLICE-APP")
tab_m, tab_f, tab_l = st.tabs(["🍽️ Menu & Courses", "🧊 Mon Frigo", "📚 Livre de Recettes"])

with tab_m:
    if st.session_state["menu_actuel"]:
        col_m, col_c = st.columns([1.2, 1])
        with col_m:
            st.subheader(f"Menu : {st.session_state['theme_actuel']}")
            for i, rec in enumerate(st.session_state["menu_actuel"]):
                with st.expander(f"**{rec['nom']}**"):
                    afficher_details_recette(rec)
                    if st.button(f"🔄 Remplacer", key=f"sw_{i}"):
                        theme = st.session_state["theme_actuel"]
                        res = supabase.table("recettes").select("id, nom, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").ilike("theme", theme).execute()
                        potentielles = [r for r in res.data if r["id"] not in [x["id"] for x in st.session_state["menu_actuel"]]]
                        if potentielles:
                            st.session_state["menu_actuel"][i] = random.choice(potentielles)
                            save_menu_supabase(st.session_state["menu_actuel"])
                            st.rerun()
        with col_c:
            st.subheader("🛒 Courses")
            crs = calculer_courses(st.session_state["menu_actuel"], nb_p)
            for r, ings in crs.items():
                st.write(f"**{r}**")
                for n, d in ings.items():
                    q = int(d['quantite']) if d['quantite'].is_integer() else round(d['quantite'], 2)
                    st.checkbox(f"{n} : {q} {d['unite']}", key=f"ch_{r}_{n}")
            
            st.divider()
            txt_exp = "🛒 *Liste de Courses*\n\n" + "".join([f"📍 *{r}*\n" + "".join([f"☐ {n} : {d['quantite']} {d['unite']}\n" for n, d in ings.items()]) + "\n" for r, ings in crs.items()])
            st.markdown(f'<a href="https://wa.me/?text={urllib.parse.quote(txt_exp)}" target="_blank" style="background:#25D366; color:white; padding:10px; border-radius:5px; text-decoration:none; display:block; text-align:center;">📱 WhatsApp</a>', unsafe_allow_html=True)
            
            if st.button("💰 Estimer budget", use_container_width=True):
                st.info(f"Budget estimé : {estimer_budget_ia(crs)}")
    else: 
        st.info("Votre menu est vide. Utilisez la barre latérale ou le livre de recettes !")

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
    
    with st.expander("➕ Ajouter une recette manuellement"):
        with st.form("manual_recipe"):
            m_nom = st.text_input("Nom du plat")
            m_theme = st.text_input("Thème (ex: Italien, Dessert...)")
            m_inst = st.text_area("Instructions de préparation")
            st.write("---")
            st.write("**Ingrédients (pour 1 personne) :**")
            m_ings_txt = st.text_area("Un ingrédient par ligne format : Nom;Quantité;Unité;Rayon", help="Exemple : Oeuf;2;pièces;Frais")
            
            if st.form_submit_button("Sauvegarder dans le livre"):
                ings_list = []
                for line in m_ings_txt.split('\n'):
                    if ';' in line:
                        p = line.split(';')
                        ings_list.append({"nom": p[0], "qte": p[1], "unite": p[2], "rayon": p[3]})
                if ajouter_recette_manuelle(m_nom, m_theme, m_inst, ings_list):
                    st.success("Recette ajoutée !")
                    st.rerun()

    st.divider()

    all_r = supabase.table("recettes").select("id, nom, theme, instructions, recette_ingredients(quantite, unite, ingredients(nom, rayon))").execute()
    if all_r.data:
        themes_presents = sorted(list(set([r['theme'] for r in all_r.data])))
        for t in themes_presents:
            st.markdown(f"### 📍 {t}")
            for r in [x for x in all_r.data if x['theme'] == t]:
                c1, c2, c3 = st.columns([4, 1, 1])
                with c1:
                    with st.expander(f"📖 {r['nom']}"):
                        afficher_details_recette(r)
                with c2:
                    if st.button("➕ Menu", key=f"add_m_{r['id']}"):
                        st.session_state["menu_actuel"].append(r)
                        save_menu_supabase(st.session_state["menu_actuel"])
                        st.toast(f"{r['nom']} ajouté au menu !")
                with c3:
                    if st.button("🗑️", key=f"del_r_{r['id']}"):
                        supabase.table("recettes").delete().eq("id", r['id']).execute()
                        st.rerun()