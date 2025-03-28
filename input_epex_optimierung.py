from gurobipy import Model, GRB, quicksum
import pandas as pd
import time
import os
import config

start = time.time() 


# ======================================================
# 1) Einlesen oder Erzeugen der Basis-Daten
# ======================================================

def modellierung_epex(szenario, strategie):
    cluster = int(szenario.split('_')[1])
    bidirektional = False if szenario.split('_')[10] == 'M' else True
    path = os.path.dirname(os.path.abspath(__file__))
    
    df_epex = pd.read_csv(os.path.join(path, 'input', 'epex_week.csv'), sep=';', decimal=',', index_col=0)
    
    df_lastgang = df_epex.copy()
    df_lastgang['Leistung'] = 0.0
    df_lastgang['Zeit_Num'] = range(0, len(df_lastgang) * 5, 5)
    
    df_lastgang.drop(['Tag', 'Uhrzeit', 'Wochentag'], axis=1, inplace=True)
    print(df_lastgang)
    
    df_lkw  = pd.read_csv(os.path.join(path, 'data', 'lkws', f'eingehende_lkws_loadstatus_{szenario}.csv'), sep=';', decimal=',', index_col=0)
    df_lkw.sort_values(by=['Ankunftszeit_total'], inplace=True)
    df_lkw.reset_index(drop=True, inplace=True)
    
    df_ladehub = pd.read_csv(os.path.join(path, 'data','konfiguration_ladehub',f'anzahl_ladesaeulen_{szenario}.csv'), sep=';', decimal=',')
    
    dict_lkw_lastgang = {
        'LKW_ID': [],
        'Ladetyp': [],
        'Zeit': [],
        'Ladezeit': [],
        'Leistung': [],
        'Pplus': [],
        'Pminus': [],
        'SOC': [],
        'z': [],
        'Preis': []
    }

    # Maximale Leistung pro Ladesäulen-Typ
    ladeleistung = {
        'NCS': int(int(szenario.split('_')[7].split('-')[0])/100 * 100),
        'HPC': int(int(szenario.split('_')[7].split('-')[1])/100 * 350),
        'MCS': int(int(szenario.split('_')[7].split('-')[2])/100 * 1000)
    }


    # Verfügbare Anzahl Ladesäulen pro Typ
    max_saeulen = {
        'NCS': int(df_ladehub['NCS'][0]),
        'HPC': int(df_ladehub['HPC'][0]),
        'MCS': int(df_ladehub['MCS'][0])
    }
 
    netzanschlussfaktor = int(int(szenario.split('_')[5])/100)
    netzanschluss = (max_saeulen['NCS'] * ladeleistung['NCS'] + max_saeulen['HPC'] * ladeleistung['HPC'] + max_saeulen['MCS'] * ladeleistung['MCS']) * netzanschlussfaktor
    
    for week in range(1):
        print(f"Optimierung Woche {week+1}")
        df_lkw_filtered = df_lkw[(df_lkw['LoadStatus'] == 1) & (df_lkw['Wochentag']>=1+week*7) & (df_lkw['Wochentag']<=7+week*7)][:].copy()
        T = 288 * 8          # = 2304
        Delta_t = 5 / 60.0   # Zeitintervall in Stunden (5 Minuten)
        # ======================================================
        # 2) Schleife über die Ladestrategien
        # ======================================================
        # --------------------------------------------------
        # 2.1) LKW-Daten vorbereiten/filtern
        # --------------------------------------------------
        df_lkw_filtered['t_a'] = ((df_lkw_filtered['Ankunftszeit_total']) // 5).astype(int)
        df_lkw_filtered['t_d'] = ((df_lkw_filtered['Ankunftszeit_total'] + df_lkw_filtered['Pausenlaenge'] - 5) // 5).astype(int)
        t_in = df_lkw_filtered['t_a'].tolist()
        t_out = df_lkw_filtered['t_d'].tolist()
        l = df_lkw_filtered['Ladesäule'].tolist()
        SOC_A = df_lkw_filtered['SOC'].tolist()
        kapazitaet = df_lkw_filtered['Kapazitaet'].tolist()
        epex_price = df_epex['Preis'].tolist()
        
        
        pow_split = szenario.split('_')[6].split('-')
        
        if len(pow_split) > 1:
            lkw_leistung_skalierung = int(pow_split[1])/100
        else:
            lkw_leistung_skalierung = 1
        max_lkw_leistung = [leistung * lkw_leistung_skalierung for leistung in df_lkw_filtered['Max_Leistung'].tolist()]

        
        SOC_req = []
        for index, row in df_lkw_filtered.iterrows():
            if row['Ladesäule'] == 'NCS':
                SOC_req.append(1)
            else:
                SOC_req.append(4.5 * 1.26 * 80 / row['Kapazitaet'] + 0.15)
        
        E_req = [kapazitaet[i] * (SOC_req[i] - SOC_A[i]) for i in range(len(df_lkw_filtered))]
        I = len(df_lkw_filtered)
        
        # --------------------------------------------------
        # 2.2) Gurobi-Modell
        # --------------------------------------------------
        model = Model("Ladehub_Optimierung")
        model.setParam('OutputFlag', 0)
        
        # --------------------------------------------------
        # 2.3) Variablen anlegen
        # --------------------------------------------------
        P = {}
        Pplus = {}
        Pminus = {}
        P_max_i = {}
        P_max_i_2 = {}
        SoC = {}
        z = {}
        
        P = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], lb=-GRB.INFINITY if bidirektional else 0, vtype=GRB.CONTINUOUS, name="P")
        Pplus = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], lb=0, vtype=GRB.CONTINUOUS, name="Pplus")
        Pminus = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], lb=0, vtype=GRB.CONTINUOUS, name="Pminus")
        P_max_i = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], lb=0, vtype=GRB.CONTINUOUS, name="Pmax_i")
        P_max_i_2 = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], lb=0, vtype=GRB.CONTINUOUS, name="Pmax_i_2")
        z = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 1)], vtype=GRB.BINARY, name="z")
        SoC = model.addVars([(i, t) for i in range(I) for t in range(t_in[i], t_out[i] + 2)], lb=0, ub=1, vtype=GRB.CONTINUOUS, name="SoC")
        
        # --------------------------------------------------
        # 2.5) Constraints
        # --------------------------------------------------
        
        # # Begrenzung Anzahl Ladesäulen
        # model.addConstrs((X[(i, t)] == 1 for i in range(I) for t in range(t_in[i], t_out[i] + 1)), name="X_equals_1")
        
        # for t in range(T):
        #     for typ in max_saeulen:
        #         relevant_i = [i for i in range(I) if l[i] == typ and (t_in[i] <= t <= t_out[i])]
        #         if len(relevant_i) > 0:
        #             model.addConstr(quicksum(X[(i, t)] for i in relevant_i) <= max_saeulen[typ],name=f"saeulen_{typ}_{t}")        
        
        # Energiebedarf je LKW decken
        for i in range(I):
            model.addConstr(quicksum(P[(i, t)] * Delta_t for t in range(t_in[i], t_out[i] + 1)) < E_req[i])        
        
        # Leistungsbegrenzung Ladekurve
        for i in range(I):
            model.addConstr(SoC[(i, t_in[i])] == SOC_A[i])
        for i in range(I):
            for t in range(t_in[i], t_out[i]+1):
                model.addConstr(SoC[(i, t+1)] == SoC[(i, t)] + (P[(i, t)] * Delta_t / kapazitaet[i]))
                
        # xvals = [0.0, 0.5, 0.5, 0.8, 0.8, 1.0]
        # yvals = [1000, 1000, 800, 800, 500, 500]
        # for i in range(I):
        #     for t in range(t_in[i], t_out[i] + 1):
        #         model.addGenConstrPWL(SoC[(i, t)], P_max_i[(i, t)],xvals, yvals)
        # for i in range(I):
        #     for t in range(t_in[i], t_out[i] + 1):
        #         model.addConstr(Pplus[(i,t)] <= P_max_i[(i,t)] * z[(i,t)]) 
        #         model.addConstr(Pminus[(i,t)] <= P_max_i[(i,t)] * (1-z[(i,t)]))

        for i in range(I):
            for t in range(t_in[i], t_out[i] + 1):
                ml = max_lkw_leistung[i]
                model.addConstr(P_max_i[(i, t)] == (-0.177038 * SoC[(i, t)] + 0.970903) * ml)
                model.addConstr(P_max_i_2[(i, t)] == (-1.51705 * SoC[(i, t)] + 1.6336) * ml)
                    
            for t in range(t_in[i], t_out[i] + 1):
                model.addConstr(Pplus[(i,t)] <= P_max_i[(i,t)] * z[(i,t)])
                model.addConstr(Pminus[(i,t)] <= P_max_i[(i,t)] * (1-z[(i,t)]))
                
                model.addConstr(Pplus[(i,t)] <= P_max_i_2[(i,t)] * z[(i,t)])
                model.addConstr(Pminus[(i,t)] <= P_max_i_2[(i,t)] * (1-z[(i,t)]))

        # Leistungsbegrenzung Ladesäulen-Typ    
        for i in range(I):
            typ = l[i]
            P_max_l = ladeleistung[typ]
            for t in range(t_in[i], t_out[i] + 1):
                model.addConstr(Pplus[(i,t)] <= z[(i,t)]     * P_max_l)
                model.addConstr(Pminus[(i,t)] <= (1-z[(i,t)]) * P_max_l)
        
        # Leistungsbegrenzung Netzanschluss
        for t in range(T):
            model.addConstr(quicksum(Pplus[(i, t)] + Pminus[(i, t)] for i in range(I) if t_in[i] <= t <= t_out[i]) <= netzanschluss)    
        
        # Hilfsbedingungen
        for i in range(I):
            for t in range(t_in[i], t_out[i]+1):
                model.addConstr(P[(i,t)] == Pplus[(i,t)] - Pminus[(i,t)])
                
            # for t in range(t_in[i], t_out[i]): # Monotoniebedingung wird nicht mehr benötigt
            #     model.addConstr(z[(i, t+1)] >= z[(i, t)])
        
        # --------------------------------------------------
        # 2.4) Zielfunktion
        # --------------------------------------------------
        
        if strategie == 'epex':
            obj_expr = quicksum(P[(i, t)] * epex_price[t] for i in range(I) for t in range(t_in[i], t_out[i] + 1))
            model.setObjective(obj_expr, GRB.MINIMIZE)
        elif strategie == 'Tmin':
            obj_expr = quicksum((t * Pplus[(i, t)]) - (t * Pminus[(i, t)]) for i in range(I) for t in range(t_in[i], t_out[i] + 1))
            model.setObjective(obj_expr, GRB.MINIMIZE)
        else:
            raise ValueError(f"Strategie {strategie} nicht bekannt.")

        # --------------------------------------------------
        # 2.6) Optimierung
        # --------------------------------------------------
        model.optimize()
        
        # --------------------------------------------------
        # 2.7) Ergebnisse in df_lastgang übernehmen
        # --------------------------------------------------
        if model.Status == GRB.OPTIMAL:
            print(f"Optimale Lösung gefunden.")
            for i in range(I):
                t_charging = 0
                for t in range(T):   
                    if t_in[i] <= t <= t_out[i]+1:
                        dict_lkw_lastgang['LKW_ID'].append(df_lkw_filtered.iloc[i]['Nummer'])
                        dict_lkw_lastgang['Zeit'].append(t*5)
                        dict_lkw_lastgang['Ladetyp'].append(l[i])
                        dict_lkw_lastgang['Ladezeit'].append(t_charging)
                        dict_lkw_lastgang['Preis'].append(epex_price[t])
                        t_charging += 5
                        if t > t_out[i]:
                            dict_lkw_lastgang['Leistung'].append(None)
                            dict_lkw_lastgang['Pplus'].append(None)
                            dict_lkw_lastgang['Pminus'].append(None)
                            dict_lkw_lastgang['SOC'].append(SoC[(i, t_out[i]+1)].X)
                            dict_lkw_lastgang['z'].append(None)
                            continue
                        else:                        
                            dict_lkw_lastgang['z'].append(z[(i, t)].X)
                            dict_lkw_lastgang['Pplus'].append(Pplus[(i, t)].X)
                            dict_lkw_lastgang['Pminus'].append(Pminus[(i, t)].X)
                            dict_lkw_lastgang['Leistung'].append(P[(i, t)].X)
                            dict_lkw_lastgang['SOC'].append(SoC[(i, t)].X)  
                            
        else:
            print(f"Keine optimale Lösung für gefunden.")
    

    df_lkw_lastgang = pd.DataFrame(dict_lkw_lastgang)
    df_lkw_lastgang.sort_values(by=['LKW_ID', 'Zeit'], inplace=True)
    
    for _, row in df_lkw_lastgang.iterrows():
        leistung = row['Leistung']        
   
        
        if leistung > 0:
            df_lastgang.loc[df_lastgang['Zeit_Num'] == row['Zeit'], 'Leistung'] += leistung
            
    
    
    cost = (P[(i, t)].X * (5/60) * epex_price[t] for i in range(I) for t in range(t_in[i], t_out[i] + 1))
    total_cost = sum(cost)
    print(f"Total cost: {total_cost} € für Strategie {strategie}")
    
    return df_lkw_lastgang, df_lastgang

def main():
    df_lkw_lastgang_main = pd.DataFrame()
    df_lastgang_main = pd.DataFrame()
    
    strategies = ['epex', 'Tmin']
    
    for szenario in config.list_szenarien:
        for strategie in strategies:
            print(f"Optimierung EPEX: {szenario}")
            df_lkw_lastgang, df_lastgang = modellierung_epex(szenario, strategie)
            df_lkw_lastgang['Strategie'] = strategie
            df_lastgang['Strategie'] = strategie
            df_lkw_lastgang_main = pd.concat([df_lkw_lastgang_main, df_lkw_lastgang])
            df_lastgang_main = pd.concat([df_lastgang_main, df_lastgang])
            
        path = os.path.dirname(os.path.abspath(__file__)) 
        df_lkw_lastgang_main.to_csv(os.path.join(path, 'data', 'lastgang_lkw_epex', f'lastgang_lkw_{szenario}.csv'), sep=';', decimal=',', index=False)
        df_lastgang_main.to_csv(os.path.join(path, 'data', 'lastgang_epex', f'lastgang_{szenario}.csv'), sep=';', decimal=',', index=True)
    
    
if __name__ == '__main__':
    start = time.time()
    main()
    end = time.time()
    
    print(f"Laufzeit: {end - start} Sekunden")
