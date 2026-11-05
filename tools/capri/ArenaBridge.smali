# Capri 1.22 adapter for the local arena-api server.
# Pairing string: arena+https://<machine>.<tailnet>.ts.net:<port>#<secret>
# Only Tailscale HTTPS hostnames are allowed; the secret is sent in a header,
# never as part of the network URL. The original cloud providers are unchanged.
.class public final Lapp/capri/ArenaBridge;
.super Ljava/lang/Object;

.method public static isAnthropic(Ljava/lang/String;)Z
    .locals 1

    const-string v0, "anthropic"
    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v0
    if-nez v0, :yes

    const-string v0, "arena"
    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v0
    return v0

    :yes
    const/4 v0, 0x1
    return v0
.end method

# The original app tries Mistral for unrecognised keys. Never send a mistyped
# Arena pairing string to that or any other external provider.
.method public static guard(Ljava/lang/String;)V
    .locals 2

    const-string v0, "arena+"
    invoke-virtual {p0, v0}, Ljava/lang/String;->startsWith(Ljava/lang/String;)Z
    move-result v0
    if-nez v0, :ok

    sget-object v0, Ljava/util/Locale;->ROOT:Ljava/util/Locale;
    invoke-virtual {p0, v0}, Ljava/lang/String;->toLowerCase(Ljava/util/Locale;)Ljava/lang/String;
    move-result-object v0
    const-string v1, "arena"
    invoke-virtual {v0, v1}, Ljava/lang/String;->startsWith(Ljava/lang/String;)Z
    move-result v1
    if-nez v1, :invalid
    const-string v1, ".ts.net"
    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z
    move-result v1
    if-nez v1, :invalid
    :ok
    return-void

    :invalid
    new-instance v0, Ljava/lang/IllegalArgumentException;
    const-string v1, "Invalid Arena pairing string"
    invoke-direct {v0, v1}, Ljava/lang/IllegalArgumentException;-><init>(Ljava/lang/String;)V
    throw v0
.end method

.method public static resolve(Lapp/capri/Provider;Ljava/lang/String;)Lapp/capri/Provider;
    .locals 6

    # Leave all existing providers untouched.
    invoke-virtual {p0}, Lapp/capri/Provider;->getId()Ljava/lang/String;
    move-result-object v0
    const-string v1, "arena"
    invoke-virtual {v1, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v0
    if-nez v0, :validate
    return-object p0

    :validate
    # This is a full-string match, not a suffix check: no user-info, path,
    # query, cleartext URL, or non-Tailscale host may be injected.
    const-string v0, "arena\\+https://[a-zA-Z0-9-]+\\.[a-zA-Z0-9-]+\\.ts\\.net:[0-9]{1,5}#[A-Za-z0-9_-]{32,128}"
    invoke-virtual {p1, v0}, Ljava/lang/String;->matches(Ljava/lang/String;)Z
    move-result v0
    if-eqz v0, :invalid

    const/16 v0, 0x23
    invoke-virtual {p1, v0}, Ljava/lang/String;->indexOf(I)I
    move-result v0
    const/4 v1, 0x6
    invoke-virtual {p1, v1, v0}, Ljava/lang/String;->substring(II)Ljava/lang/String;
    move-result-object v2

    new-instance v3, Ljava/net/URI;
    invoke-direct {v3, v2}, Ljava/net/URI;-><init>(Ljava/lang/String;)V
    invoke-virtual {v3}, Ljava/net/URI;->getPort()I
    move-result v3
    if-lez v3, :invalid
    const v0, 0xffff
    if-gt v3, v0, :invalid

    new-instance v0, Ljava/lang/StringBuilder;
    invoke-direct {v0, v2}, Ljava/lang/StringBuilder;-><init>(Ljava/lang/String;)V
    const-string v2, "/v1"
    invoke-virtual {v0, v2}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;
    invoke-virtual {v0}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;
    move-result-object v4

    new-instance v1, Lapp/capri/Provider;
    const-string v2, "arena"
    const-string v3, "Arena"
    const-string v5, "arena+"
    invoke-static {v5}, Ljava/util/Collections;->singletonList(Ljava/lang/Object;)Ljava/util/List;
    move-result-object v5
    invoke-direct {v1, v2, v3, v4, v5}, Lapp/capri/Provider;-><init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/util/List;)V
    return-object v1

    :invalid
    new-instance v0, Ljava/lang/IllegalArgumentException;
    const-string v1, "Invalid Arena pairing string"
    invoke-direct {v0, v1}, Ljava/lang/IllegalArgumentException;-><init>(Ljava/lang/String;)V
    throw v0
.end method

.method public static secret(Ljava/lang/String;)Ljava/lang/String;
    .locals 1

    const/16 v0, 0x23
    invoke-virtual {p0, v0}, Ljava/lang/String;->indexOf(I)I
    move-result v0
    add-int/lit8 v0, v0, 0x1
    invoke-virtual {p0, v0}, Ljava/lang/String;->substring(I)Ljava/lang/String;
    move-result-object v0
    return-object v0
.end method
