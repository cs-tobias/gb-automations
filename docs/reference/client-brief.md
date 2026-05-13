Vi må nok sette opp en docker desktop hvor vi bygger alt dette. Virker som det er lønnsomt både for MCP server og N8n evt hvis man skal bruke det.

[Ref på MCP server - både lage egen og connecte ferdige](https://www.youtube.com/watch?v=GuTcle5edjk&t=160s)

# Generell plan

Målet er å automatisere informasjonsflyt fra alle prosesser i produksjonen inn til notion. Når notion blir “single source of thruth” kan vi også bruke notion som kontekst for AI prosesser.

Ved å ha en komplett kontekst med all informasjon vil vi være fremtidsrettet til den nye AI hverdagen. Jo bedre kontekst man kan gi AI, jo bedre vil den kunne hjelpe etterhvert som den blir smartere. 

En ting å tenkte på er struktureringen av konteksten. Etter hvert vil det ligge så mye informasjon i notion at kontekstvinduet blir for stort. For å løse dette må vi kanskje se litt på RAG løsninger. Evt llm VIKI. Det kan også være at man til slutt kutter ut notion helt og at prosjektstyringen blir direkte i AI. Jeg vet ikke hvordan det ville fungert, men teoretisk er notion bare en UI for oss til å forstå informasjonen.

<aside>
💡

RAG and the LLM Wiki pattern represent different approaches to knowledge management: **RAG (Retrieval-Augmented Generation) is ideal for searching large, dynamic datasets, whereas the LLM Wiki (popularized by Andrej Karpathy) builds a persistent, curated markdown knowledge base that improves over time, reducing token use by up to 95%**. RAG works best for massive, frequently changing data; Wiki is better for smaller, high-quality curated knowledge.

</aside>

### Eksempler på bruks tilfeller:

- Booking - AI kan hjelpe med booking av oppgaver når den har tilgang på kontekst om hvem som passer hvilken oppgaver best, hvem som ligger forran eller bak med sitt. Dette vil gjøre timeplanen riktigere til en hver tid og det vil være mulig å flagge leveranser som ikke er mulige å levere til deadline tidligere enn i dag. Vi vil også spare mye tid på det manuelle arbeidet rundt booking.
- Rapportere mangler i underlaget eller spørsmål som må besvares. Ved å snakke med de ansatte kan AI-en lage oversikter på ting vi mangler eller ting som bør defineres. Mailen kan gjøres klar slik at prosjektleder bare ser over og sender videre.
- Analyse - Med kontekst om budsjett, timebruk og type oppgaver kan ai gjøre analyse av hvilken oppgaver som er best økonomisk. Eller flagge oppgaver vi ofte taper tid på slik at vi øker budsjettet eller løser problemer i prosess.
- Prosjektlogg - Med kontekst fra status på oppgaver, alle mailer, møtenotater osv. kan ai oppsummere status i prosjektet for viktige hendelser eller endringer som skjer underveis. Dette er spesielt nyttig i prosjekter der vi er flere som er i kontakt med kunden. Da hender det vi mister oversikt på hva som er sagt fra hver enkelt.
- Har veldig mange flere ideer her, men dette er noen av de for å gi litt insikt i hva planen er langsiktig

# Gmail integrasjon

### **Kort forklaring**

Når prosjekter opprettes i Notion ønsker jeg at prosjektet også opprettes som en etikett i Gmail. Etiketten må opprettes i alle Goldbox-mailene, ikke bare på min.

Når noen får mail som tilhører et prosjekt, så legger vi mailen i etiketten. Mailer som blir lagt i prosjektetiketten vil synkes inn til Notion i en mail-database som linkes til prosjektene.

### Ting som hadde vært nice:

- Når nye personer er i mailtråden kan vi automatisk hente ut kontaktpersoner med informasjon om hvor de jobber, tlf og mailadresse. (Vi har en database i notion som er kontaktpersoner. Hittil har vi måtte oprette kontaktpersoner manuelt, så vi har nesten sluttet med å gjøre dette nå. men det hadde vært fint å ha denne informasjonen i notion også)
- For å ha en struktur på mailene i notion må vi kanskje bruke hver mailtråd (emne) som gruppering. Bare så man forstår hvilken samtale man leser når man ser i notion. Det kunne også vært fint med automatisk tagging slik at man kan søke etter “tilbud” eller “korreksjon”. Så får man mailer som passer disse kategoriene.

### Potensielle problemer

- Vi må passe på at Mailtrådene føres over på en ryddig måte slik at det ikke blir mailer som ligger dobbelt, eller at korrespondansen kommer opp i feil ift når hver mail ble sendt osv.
- Hvis det er bilder innebygget i mailen og ikke som vedlegg. Hvordan kan vi passe på at mailen blir vist på samme måte i notion oversikten? (Bare viktig å unngå at man missforstår hvis ting ikke vises slik som det ble sendt)
- 

# Frame integrasjon

### Kort forklaring

Når et prosjekt opprettes i notion og blir aktivt (Det er ikke aktivt når det bare er i tilbudsfase. Men så fort tilbudet er godkjent blir det et aktivt prosjekt) så opprettes også prosjektet i Frame. Det er viktig at frame speiler notion. Så hvis prosjektet endrer navn i notion senere må det også reflekteres i frame.

Når vi oppretter oppgaver i notion. Eks 3 eksteriør og 4 interiør. Så lages det en mappestruktur og placeholder filer i frame. Når førsteutkastet er klart så legger vi det over placeholderen slik at første utkastet ligger som V2 i frame. Grunnen til dette er for å få bedre struktur i frame. I dag lager alle mapper og filer på forskjellige måter. Vi må få litt mer struktur på dette så det blir likt hver gang. Det vil også gjøre det lettere å linke oppgaven i notion til bildet i frame fra starten og til ferdig leveranse.

Når kunde eller vi kommenterer i frame så synker vi alle korreksjoner inn i notion. Korreksjoner kan være en egen database der vi linker hver kommentar til prosjekt og oppgave.

### ting som hadde vært nice

- Når korreksjoner kommer på et bilde blir det automatisk opprettet en underoppgave til hovedoppgaven i notion som heter korreksjon runde 1, korreksjon runde 2 osv. Det er fint at korreksjoner blir egne underopppgaver slik at vi booker tid også på korreksjoner.
- Hvis en kunde stiller spørsmål i frame kommentarene. Eks, skal det være treverk her? Hvis AI-en ser i en mailtråd, eller i underlaget at det er spesifisert. Så kan ai svare basert på konteksten at dette er snakket om i denne mailen eller at det er spesifisert i dette dokumentet i underlaget.
    - Dette bør ikke skje helt automatisk før det svares på kommentaren. En prosjektleder bør godkjenne svaret før det legges inn.
- Når prosjektet registreres som ferdig i notion kan prosjektet settes som inaktivt i frame.
- Når man opprettes en placeholderfil i frame kan man legge til linken i notion slik at man kan trykke seg rett inn til bildet om man vil se hva som ligger på frame.
- Basert på kommentarer i frame, emailer osv så kan det ligge en endringslogg i hver oppgave.

### potensielle problemer

- 

# Toggl integrasjon

### Kort forklaring

Når et prosjekt opprettes i notion og blir aktivt (Det er ikke aktivt når det bare er i tilbudsfase. Men så fort tilbudet er godkjent blir det et aktivt prosjekt) så opprettes også prosjektet i toggl. Det er viktig at toggl speiler notion. Så hvis prosjektet endrer navn i notion senere må det også reflekteres i toggl.

En gang hver dag så synkes timene inn fra toggl til notion. Notion sine databaser tåler ikke enorme mengder innlegg i databasen. For å forhindre dette kan vi gjøre følgende:

Når man fører timer på et prosjekt iløpet av en dag vil det kanskje være 4+ føringer avhengig av hvor mye start og stopp det blir iløpet av dagen. Istedenfor å legge inn alle disse, så samler vi hver dag pr ansatt, pr prosjekt. Det som føres over til notion blir bare en samlet timeregistrering på totale timer pr ansatt i prosjektet for hver dag.

### ting som hadde vært nice

- Timer fra toggl skal også brukes til grunnlag for lønninger, overtid og undertid. For dette så trenger vi egentlig bare totale timene ila mnd.

### potensielle problemer

- Hvordan kan vi forsikre oss om at dette blir riktig hvis noen sletter eller endrer en timeføring i ettertid?

# Fiken integrasjon

### Kort forklaring

### ting som hadde vært nice

- 

### potensielle problemer

- 

# Møte integrasjon

### Kort forklaring

For å få all informasjonen inn i notion er det også viktig at møter som skjer fysisk eller på nett kommer med. For å gjøre dette må vi ha en rutine på å ta notater eller å ta opp lyd fra møter som kan transkriberes. Jeg har testet noen forskjellige transcribe apps til nettmøter. Men har ikke funnet noe jeg liker godt ennå. Firefly er kanskje nærmest. 

Men jeg tror vi lett kan transcribe audio selv sålenge vi tar opp lyd i møter.

### ting som hadde vært nice

- 

### potensielle problemer

-