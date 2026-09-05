[out:json][timeout:300];
(
    nwr[~"^lgbtq(:.*)?$"~"^(yes|only|primary|welcome)$",i];
    nwr[~"^non_binary"~"^(yes|only|primary|welcome)$",i]
    nwr[~"^(male|female):transgender$"~"^(yes|only|primary|welcome)$",i];
);
out center geom;
